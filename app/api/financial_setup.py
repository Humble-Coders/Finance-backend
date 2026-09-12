"""The financial setup wizard's endpoints (PRD F1).

Two calls, thin over `app/services/financial_setup.py`:

    GET  /financial-setup        what is saved
    PUT  /financial-setup        save (call it after every step)

Both refuse while the wizard's **prerequisites** are outstanding: the amounts
need a currency, and the currency comes from the region the phone step
establishes (#24).

They must NOT refuse for `financial_setup` itself, even though it is an
onboarding step (#29). Saving here is how that step gets cleared, so gating
these two on it would lock the user out of the only endpoint that can clear it.
Every other endpoint gates on the whole list via `require_onboarded`.

There is no skip call: the optional half records a skipped answer as no row,
which is what an unasked one looks like too.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity
from app.db import get_session
from app.schemas.financial_setup import FinancialSetupIn, FinancialSetupOut
from app.services.financial_setup import (
    SetupValidationError,
    get_setup,
    save_setup,
)
from app.services.identity import ResolvedIdentity
from app.services.onboarding import (
    onboarding_conflict,
    onboarding_state,
    wizard_prerequisites,
)

router = APIRouter(tags=["financial setup"])

INVALID_AMOUNT = "invalid_amount"


async def _require_prerequisites(
    session: AsyncSession, identity: ResolvedIdentity
) -> None:
    """Phone, region and consent — but never `financial_setup` itself.

    Pinned by a test: these endpoints have to keep working while that step is
    the only one outstanding, or it can never be cleared.
    """
    state = await onboarding_state(session, identity.user, identity.household)
    outstanding = wizard_prerequisites(state)
    if outstanding:
        raise onboarding_conflict(outstanding)


@router.get("/financial-setup", response_model=FinancialSetupOut)
async def read_setup(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> FinancialSetupOut:
    await _require_prerequisites(session, identity)
    return await get_setup(session, identity.household)


@router.put("/financial-setup", response_model=FinancialSetupOut)
async def write_setup(
    body: FinancialSetupIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> FinancialSetupOut:
    """Replace the wizard's data. Idempotent, so it is safe after every step."""
    await _require_prerequisites(session, identity)
    try:
        return await save_setup(session, identity.household, body)
    except SetupValidationError as exc:
        # Named field, so the client can highlight the row the user typed.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": INVALID_AMOUNT,
                "field": exc.field,
                "message": exc.message,
            },
        ) from exc
