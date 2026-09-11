"""Response shapes for identity endpoints.

Declared explicitly rather than serialising ORM objects, so the wire contract is
a deliberate decision — the mobile clients generate against it, and leaking a
column by accident is how internal fields become public API.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict

__all__ = ["ConsentIn", "HouseholdOut", "MeOut", "RegionIn", "TermsStatus", "UserOut"]


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


class TermsStatus(BaseModel):
    # The account-terms version in force, and whether this user has accepted it.
    # `version` is None only when no terms are configured.
    version: str | None
    accepted: bool


class MeOut(BaseModel):
    user: UserOut
    household: HouseholdOut

    # What the client must still collect, in routing order: "phone", "region",
    # "consent". Empty once nothing is outstanding. Never blocks the request —
    # the client reads it and routes (app/services/onboarding.py).
    onboarding_required: list[str] = []

    terms: TermsStatus


class RegionIn(BaseModel):
    # ISO 3166-1 alpha-2, any case. Validated against libphonenumber's region
    # list in the endpoint, so an unknown code gets a coded 422.
    country_code: str


class ConsentIn(BaseModel):
    # The terms version the user was shown. Must be the one in force.
    version: str
