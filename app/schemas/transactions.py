"""Request and response shapes for the review queue."""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel

from app.models.enums import ReviewReason, TransactionDirection

__all__ = ["ReviewRowOut", "ReviewPageOut", "DuplicateOfOut"]


class DuplicateOfOut(BaseModel):
    """The row a suspected duplicate collided with.

    Sent alongside the flagged row rather than left for a second request: the
    screen is asking "is this the same as that?", and a question posed without
    the evidence is one the user can only answer by guessing.
    """

    id: uuid.UUID
    occurred_on: date
    # Decimal string, per the money boundary (PRD §4.4).
    amount: str
    description: str | None = None


class ReviewRowOut(BaseModel):
    id: uuid.UUID
    account_id: uuid.UUID
    occurred_on: date
    amount: str
    currency: str
    direction: TransactionDirection
    description: str | None = None
    merchant: str | None = None
    category_id: uuid.UUID | None = None
    # Why this row is here. Each reason wants a different affordance, and
    # without it the queue is one undifferentiated list in which the user
    # re-reads rows that were never in doubt.
    review_reason: ReviewReason | None = None
    # 0-100 from extraction, or null for a row that was never modelled.
    extraction_confidence: int | None = None
    duplicate_of: DuplicateOfOut | None = None


class ReviewPageOut(BaseModel):
    rows: list[ReviewRowOut]
    # Opaque: the client stores it and sends it back. Anything it could read
    # here would become something it might construct, and a hand-built cursor
    # is how paging starts skipping rows.
    next_cursor: str | None = None
