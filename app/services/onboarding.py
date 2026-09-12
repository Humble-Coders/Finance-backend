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
* ``financial_setup`` — monthly income or monthly expense is missing. Without
  both, the dashboard has nothing to reason from, so the app holds the user at
  the wizard rather than opening onto an empty product (PRD §9, 2026-09-12).

The list is what the client routes on; it never infers a step itself. Reporting
a step is not the same as refusing a request — `onboarding_state` blocks
nothing. Endpoints that must refuse depend on `require_onboarded`, and the
wizard's own endpoints refuse on `wizard_prerequisites` instead, or clearing
``financial_setup`` would require an endpoint that ``financial_setup`` blocks.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.db import get_session
from app.models.enums import PolicyKind
from app.models.identity import ConsentEvent, Household, User
from app.models.platform import DisclaimerVersion
from app.models.setup import FinancialProfile
from app.services.identity import ResolvedIdentity

__all__ = [
    "ONBOARDING_CONSENT",
    "ONBOARDING_FINANCIAL_SETUP",
    "ONBOARDING_PHONE",
    "ONBOARDING_REGION",
    "ONBOARDING_REQUIRED",
    "OnboardingState",
    "current_terms",
    "has_accepted",
    "onboarding_state",
    "require_onboarded",
    "wizard_prerequisites",
]

ONBOARDING_PHONE = "phone"
ONBOARDING_REGION = "region"
ONBOARDING_CONSENT = "consent"
ONBOARDING_FINANCIAL_SETUP = "financial_setup"

# The error code every onboarding refusal carries, so a client has one shape to
# handle rather than one per endpoint.
ONBOARDING_REQUIRED = "onboarding_required"


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

    if await needs_financial_setup(session, household):
        steps.append(ONBOARDING_FINANCIAL_SETUP)

    return OnboardingState(steps=steps, terms=terms, terms_accepted=accepted)


async def needs_financial_setup(session: AsyncSession, household: Household) -> bool:
    """True until both mandatory figures are saved (PRD §9, 2026-09-12).

    Read from the columns themselves, not from a status flag. A flag has to be
    kept in sync with the figures and can disagree with them; the figures are
    what the dashboard actually needs, so they are what the gate asks about.

    Listed even when earlier steps are also outstanding — a brand-new caller
    sees ``["phone", "financial_setup"]``. The list is ordered and the client
    routes to the first, so this stays one rule rather than a rule plus an
    exception.
    """
    result = await session.execute(
        select(
            FinancialProfile.monthly_income_minor_units,
            FinancialProfile.monthly_expense_minor_units,
        ).where(FinancialProfile.household_id == household.id)
    )
    row = result.first()
    return (
        row is None
        or row.monthly_income_minor_units is None
        or (row.monthly_expense_minor_units is None)
    )


def wizard_prerequisites(state: OnboardingState) -> list[str]:
    """The outstanding steps that block the setup wizard itself.

    Everything except `financial_setup`, which the wizard exists to clear —
    requiring that one would lock the user out of the only endpoint that can
    clear it.

    Deliberately a denylist of one rather than an allowlist of the other three.
    A step added to `onboarding_state` later blocks the wizard until someone
    decides otherwise, which is the safe default; an allowlist would let a new
    step through silently, and nothing would fail to say so.
    """
    return [step for step in state.steps if step != ONBOARDING_FINANCIAL_SETUP]


def onboarding_conflict(steps: list[str]) -> HTTPException:
    """409 naming what is still outstanding, so the client can route on it."""
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "code": ONBOARDING_REQUIRED,
            "message": "Finish onboarding first.",
            "onboarding_required": steps,
        },
    )


async def require_onboarded(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Refuse until every onboarding step is done, financial setup included.

    The counterpart to `require_feature` (app/services/capabilities.py): the
    capabilities payload decides what a client SHOWS, this decides what the API
    ALLOWS. A client is not a security boundary — anyone holding a valid token
    can call an endpoint directly — so the gate lives here, not in the apps.

    Nothing in M2 needs it yet; it exists now, proven on a throwaway route, so
    later endpoints depend on an established pattern instead of retrofitting
    enforcement after the fact.
    """
    state = await onboarding_state(session, identity.user, identity.household)
    if state.steps:
        raise onboarding_conflict(state.steps)
