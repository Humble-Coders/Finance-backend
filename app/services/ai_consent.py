"""Consent to AI processing of financial data.

Its own policy, its own version, its own consent event — deliberately not part
of the account terms. PRD Appendix A.5 #1 requires consent here to be *express*
and *unbundled*, and a checkbox that covers "the terms and also we send your
statements to an AI company" is neither.

The gate lives at the parse endpoint rather than in the client, for the same
reason every other gate does: a client is not a security boundary.
"""

from __future__ import annotations

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ConsentAction, PolicyKind
from app.models.identity import ConsentChange, ConsentEvent, User
from app.models.platform import DisclaimerVersion

__all__ = [
    "current_policy",
    "has_consented",
    "record_consent",
    "withdraw",
    "CONSENT_REQUIRED",
]

# The code the client routes on. Kept identical to the string in tickets 3.1
# and 3.6 — a code the apps were written against is a contract, and the
# cheapest place to break it is in the one repo that can see both.
CONSENT_REQUIRED = "consent_required"


async def current_policy(session: AsyncSession) -> DisclaimerVersion | None:
    """The AI-processing policy in force, or None if none is configured.

    Same rule as the account terms, and it has to be *exactly* the same rule:
    only a dated version whose date **has passed** counts. An undated row is a
    draft that can be reviewed without anyone being asked to agree to it, and a
    future-dated one is an announcement.

    Without the `<= now()` test, seeding next month's policy would take effect
    the moment it was inserted — instantly invalidating every consent already
    given, refusing every import, and pointing users at text that is not live
    yet. The `created_at` tiebreak is here for the same reason it is in
    `current_terms`: two rows sharing an effective date must not resolve
    differently from one request to the next.
    """
    result = await session.execute(
        select(DisclaimerVersion)
        .where(
            DisclaimerVersion.kind == PolicyKind.ai_processing,
            DisclaimerVersion.country_code.is_(None),
            DisclaimerVersion.effective_from.is_not(None),
            DisclaimerVersion.effective_from <= func.now(),
        )
        .order_by(
            DisclaimerVersion.effective_from.desc(),
            DisclaimerVersion.created_at.desc(),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def has_consented(
    session: AsyncSession, user: User, policy: DisclaimerVersion
) -> bool:
    """Whether this user has agreed to exactly this version, and not since
    withdrawn it.

    Version-specific on purpose. If the policy changes — a new provider, a
    different category of data — consent to the old text is not consent to the
    new one, and silently carrying it forward is the failure Appendix A exists
    to prevent.

    A withdrawal (#42) outranks every consent before it, whichever version it
    was given to; consent given again afterwards outranks the withdrawal. The
    latest `consent_change` decides, and with none at all — consent given
    before withdrawal existed — `consent_event` alone does, as it always did.
    """
    latest = await _latest_change(session, user, policy.kind)
    if latest is not None and latest.action is ConsentAction.withdrawn:
        return False
    return await _agreed_to(session, user, policy)


async def record_consent(
    session: AsyncSession, user: User, policy: DisclaimerVersion
) -> bool:
    """Record that this user agreed to this version. Returns whether it changed
    anything; agreeing when already agreed is a no-op.

    Two rows, for two different questions: `consent_event` proves which text
    was agreed to (one per version, never touched again), and a `given` change
    puts it in sequence — which is what lets consent come back after a
    withdrawal, even to the very version that was withdrawn. Nothing is
    committed here.
    """
    await _lock(session, user)
    if await has_consented(session, user, policy):
        return False
    await session.execute(
        pg_insert(ConsentEvent)
        .values(user_id=user.id, disclaimer_version_id=policy.id)
        .on_conflict_do_nothing(index_elements=["user_id", "disclaimer_version_id"])
    )
    session.add(
        ConsentChange(
            user_id=user.id,
            kind=policy.kind,
            action=ConsentAction.given,
            disclaimer_version_id=policy.id,
            created_at=func.clock_timestamp(),
        )
    )
    await session.flush()
    return True


async def withdraw(
    session: AsyncSession, user: User, kind: PolicyKind = PolicyKind.ai_processing
) -> bool:
    """Withdraw this user's consent to `kind`. Returns whether it changed
    anything.

    Idempotent (#42): withdrawing twice, or without ever having consented, is
    not an error and records nothing — a log line saying "withdrew" for someone
    who had nothing to withdraw would be a record of something that did not
    happen. Nothing is deleted: the consent that was given stays readable, and
    the withdrawal is written after it. Nothing is committed here.
    """
    await _lock(session, user)
    latest = await _latest_change(session, user, kind)
    if latest is not None:
        if latest.action is ConsentAction.withdrawn:
            return False
    elif not await _ever_agreed(session, user, kind):
        return False
    session.add(
        ConsentChange(
            user_id=user.id,
            kind=kind,
            action=ConsentAction.withdrawn,
            created_at=func.clock_timestamp(),
        )
    )
    await session.flush()
    return True


async def _lock(session: AsyncSession, user: User) -> None:
    """Serialize one person's consent changes.

    Two taps on "withdraw", or a withdraw racing an accept, would otherwise
    both read the same latest state and both write — two withdrawals in the
    log, or a consent recorded under a withdrawal it did not see. Holding the
    user's row until the transaction ends makes the second one read what the
    first wrote.
    """
    await session.execute(select(User.id).where(User.id == user.id).with_for_update())


async def _latest_change(
    session: AsyncSession, user: User, kind: PolicyKind
) -> ConsentChange | None:
    result = await session.execute(
        select(ConsentChange)
        .where(ConsentChange.user_id == user.id, ConsentChange.kind == kind)
        # Written with `clock_timestamp()`, not the column's `now()` default:
        # `now()` is fixed for a whole transaction, so two changes inside one
        # would tie and a random uuid would pick the "latest". The lock keeps
        # two requests from interleaving; this keeps one transaction in order.
        .order_by(ConsentChange.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _agreed_to(
    session: AsyncSession, user: User, policy: DisclaimerVersion
) -> bool:
    result = await session.execute(
        select(
            exists().where(
                ConsentEvent.user_id == user.id,
                ConsentEvent.disclaimer_version_id == policy.id,
            )
        )
    )
    return bool(result.scalar())


async def _ever_agreed(session: AsyncSession, user: User, kind: PolicyKind) -> bool:
    """Any version of this kind, ever — consent given before `consent_change`
    existed has no row there to find."""
    result = await session.execute(
        select(
            exists().where(
                ConsentEvent.user_id == user.id,
                ConsentEvent.disclaimer_version_id == DisclaimerVersion.id,
                DisclaimerVersion.kind == kind,
            )
        )
    )
    return bool(result.scalar())
