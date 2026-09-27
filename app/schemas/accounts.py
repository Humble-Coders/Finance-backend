"""Request and response shapes for financial accounts."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field

from app.models.enums import AccountKind

__all__ = ["AccountIn", "AccountOut"]


class AccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    kind: AccountKind
    # Optional: the household's own currency is the default, and v1 is CAD-only
    # in practice. Named explicitly rather than assumed because the money
    # boundary carries a currency with every amount (PRD §4.4).
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    institution: str | None = Field(default=None, max_length=255)


class AccountOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: AccountKind
    currency: str
    institution: str | None = None

    model_config = {"from_attributes": True}
