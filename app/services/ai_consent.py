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
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PolicyKind
from app.models.identity import ConsentEvent, User
from app.models.platform import DisclaimerVersion

__all__ = ["current_policy", "has_consented", "CONSENT_REQUIRED"]

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
    """Whether this user has agreed to exactly this version.

    Version-specific on purpose. If the policy changes — a new provider, a
    different category of data — consent to the old text is not consent to the
    new one, and silently carrying it forward is the failure Appendix A exists
    to prevent.
    """
    result = await session.execute(
        select(
            exists().where(
                ConsentEvent.user_id == user.id,
                ConsentEvent.disclaimer_version_id == policy.id,
            )
        )
    )
    return bool(result.scalar())
