"""Response shapes for legal copy."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

__all__ = ["TermsOut"]


class TermsOut(BaseModel):
    version: str
    body: str
    effective_from: datetime | None
