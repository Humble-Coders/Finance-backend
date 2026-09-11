"""What a signed-in user still owes before onboarding is complete.

One rule, read by both `/me` and `/capabilities`. They used to decide
separately — `/me` from the phone, `/capabilities` from the region — and
disagreed about a user with a verified phone (seen live in
Humble-Coders/FinAI-Mobile-2026#6). Two sources for one answer is how that
happens, so there is one.

The steps, in the order the client routes through them:

* ``phone``   — no verified phone yet: a Google or Apple sign-in, before the
  phone step (PRD §4.6).
* ``region``  — a phone, but not one libphonenumber could place, so the user
  picks their country instead of us guessing it.
* ``consent`` — the account terms in force have not been accepted (PRD
  Appendix A.5, item 1).

Never blocking: every endpoint still answers. The client reads the list and
routes to the first step.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import PolicyKind
from app.models.identity import ConsentEvent, Household, User
from app.models.platform import DisclaimerVersion

__all__ = [
    "ONBOARDING_CONSENT",
    "ONBOARDING_PHONE",
    "ONBOARDING_REGION",
    "OnboardingState",
    "current_terms",
    "has_accepted",
    "onboarding_state",
]

ONBOARDING_PHONE = "phone"
ONBOARDING_REGION = "region"
ONBOARDING_CONSENT = "consent"


@dataclass(frozen=True)
class OnboardingState:
    steps: list[str]
    # The account terms in force, or None if none are configured.
    terms: DisclaimerVersion | None
    terms_accepted: bool


async def current_terms(session: AsyncSession) -> DisclaimerVersion | None:
    """The account terms in force: the newest version that has taken effect.

    Only a dated version whose date has passed is in force. An undated row is a
    draft: it can be loaded and reviewed without anyone being asked to accept
    it, and it becomes the terms only when it is given a date.

    Global in v1 (`country_code` NULL). Per-market terms would add a country
    match here; nothing else would change.
    """
    result = await session.execute(
        select(DisclaimerVersion)
        .where(
            DisclaimerVersion.kind == PolicyKind.account_terms,
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


async def has_accepted(
    session: AsyncSession, user: User, terms: DisclaimerVersion
) -> bool:
    result = await session.execute(
        select(
            exists().where(
                ConsentEvent.user_id == user.id,
                ConsentEvent.disclaimer_version_id == terms.id,
            )
        )
    )
    return bool(result.scalar())


async def onboarding_state(
    session: AsyncSession, user: User, household: Household
) -> OnboardingState:
    steps: list[str] = []
    if not user.phone:
        steps.append(ONBOARDING_PHONE)
    elif household.country_code is None:
        # Only once there IS a phone: without one, the region comes from the
        # phone step, not from asking.
        steps.append(ONBOARDING_REGION)

    terms = await current_terms(session)
    accepted = terms is not None and await has_accepted(session, user, terms)
    # No terms version configured means there is nothing to consent to — not a
    # reason to hold every user in onboarding.
    if terms is not None and not accepted:
        steps.append(ONBOARDING_CONSENT)

    return OnboardingState(steps=steps, terms=terms, terms_accepted=accepted)
