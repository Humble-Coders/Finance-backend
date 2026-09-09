"""GET /me — who the caller is, and what onboarding they still owe.

Creates the user and household on first call. A GET with side effects is
unconventional; it is a get-or-create, it is idempotent, and it saves every
client a separate bootstrap round trip on the path users hit most.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import current_identity
from app.schemas.identity import HouseholdOut, MeOut, UserOut
from app.services.identity import ResolvedIdentity

router = APIRouter(tags=["identity"])

ONBOARDING_PHONE = "phone"


@router.get("/me", response_model=MeOut)
async def read_me(identity: ResolvedIdentity = Depends(current_identity)) -> MeOut:
    outstanding: list[str] = []
    if not identity.user.phone:
        # Google and Apple users authenticate before providing one. The request
        # succeeds; the client reads this and routes to the phone step.
        outstanding.append(ONBOARDING_PHONE)

    return MeOut(
        user=UserOut.model_validate(identity.user),
        household=HouseholdOut.model_validate(identity.household),
        onboarding_required=outstanding,
    )
