"""The capabilities payload.

Both mobile clients render their entire UI from this. The shape was fixed while
the resolver was still a stub precisely so the clients could build against it —
so treat it as a published contract, not an internal structure.
"""

from __future__ import annotations

from pydantic import BaseModel

__all__ = ["Feature", "Capabilities"]


class Feature(BaseModel):
    enabled: bool
    # Why it is off, so a hidden feature can explain itself rather than simply
    # vanishing: "coming_soon", "not_in_plan", "region_unsupported".
    reason: str | None = None


class Capabilities(BaseModel):
    # NULL until the phone step completes; never guessed (PRD §4.6).
    region: str | None
    currency: str
    locale: str
    features: dict[str, Feature]
    content: dict[str, object]

    # What the client must still collect before the payload can be complete.
    onboarding_required: list[str] = []
