"""Response shapes for identity endpoints.

Declared explicitly rather than serialising ORM objects, so the wire contract is
a deliberate decision — the mobile clients generate against it, and leaking a
column by accident is how internal fields become public API.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict

__all__ = ["HouseholdOut", "UserOut", "MeOut"]


class HouseholdOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # NULL until the phone step completes; never guessed (PRD §4.6).
    country_code: str | None


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str | None
    phone: str | None
    display_name: str | None


class MeOut(BaseModel):
    user: UserOut
    household: HouseholdOut

    # What the client must still collect. Empty once nothing is outstanding.
    # The request is never blocked on it — the client reads this and routes to
    # the phone step (PRD §4.6).
    onboarding_required: list[str] = []
