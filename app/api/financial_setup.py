"""The financial setup wizard's endpoints (PRD F1).

Three calls, thin over `app/services/financial_setup.py`:

    GET  /financial-setup        what is saved, and whether it is done
    PUT  /financial-setup        save (call it after every step)
    POST /financial-setup/skip   the user skipped it

All three refuse until onboarding is complete: the amounts need a currency, and
the currency comes from the region the phone step establishes (#24).
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
    skip_setup,
)
from app.services.identity import ResolvedIdentity
from app.services.onboarding import onboarding_state

router = APIRouter(tags=["financial setup"])

ONBOARDING_REQUIRED = "onboarding_required"
INVALID_AMOUNT = "invalid_amount"


async def _require_onboarded(session: AsyncSession, identity: ResolvedIdentity) -> None:
    state = await onboarding_state(session, identity.user, identity.household)
    if state.steps:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": ONBOARDING_REQUIRED,
                "message": "Finish onboarding before saving financial setup.",
                "onboarding_required": state.steps,
            },
        )


@router.get("/financial-setup", response_model=FinancialSetupOut)
async def read_setup(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> FinancialSetupOut:
    await _require_onboarded(session, identity)
    return await get_setup(session, identity.household)


@router.put("/financial-setup", response_model=FinancialSetupOut)
async def write_setup(
    body: FinancialSetupIn,
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> FinancialSetupOut:
    """Replace the wizard's data. Idempotent, so it is safe after every step."""
    await _require_onboarded(session, identity)
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


@router.post("/financial-setup/skip", response_model=FinancialSetupOut)
async def skip(
    identity: ResolvedIdentity = Depends(current_identity),
    session: AsyncSession = Depends(get_session),
) -> FinancialSetupOut:
    await _require_onboarded(session, identity)
    return await skip_setup(session, identity.household)
