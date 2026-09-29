"""Request and response shapes for the review queue."""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import ReviewReason, TransactionDirection
from app.schemas.statements import amount_not_negative, date_within_living_memory

__all__ = [
    "ReviewRowOut",
    "ReviewPageOut",
    "DuplicateOfOut",
    "TransactionPatchIn",
    "TransactionOut",
    "PatchOutcomeOut",
    "ConfirmIn",
    "ConfirmOutcomeOut",
    "DeleteOutcomeOut",
]


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


class TransactionPatchIn(BaseModel):
    """A person's correction to one row. Only the fields given a value change.

    A field sent as null is treated as not sent. Nothing here can be cleared:
    a correction replaces a wrong value with a right one, and "this row has no
    date" or "no amount" is not an answer the review screen offers.
    """

    occurred_on: date | None = None
    amount: str | None = None
    direction: TransactionDirection | None = None
    description: str | None = Field(default=None, min_length=1, max_length=512)
    merchant: str | None = Field(default=None, min_length=1, max_length=255)
    category_id: uuid.UUID | None = None

    @field_validator("amount")
    @classmethod
    def _not_negative(cls, value: str | None) -> str | None:
        return None if value is None else amount_not_negative(value)

    @field_validator("occurred_on")
    @classmethod
    def _within_living_memory(cls, value: date | None) -> date | None:
        return None if value is None else date_within_living_memory(value)

    @field_validator("merchant")
    @classmethod
    def _one_way_to_write_a_name(cls, value: str | None) -> str | None:
        """Single spaces, no padding — the form import already writes.

        A merchant typed as "Cafe  Luna" or with a non-breaking space would
        otherwise be stored as typed and shown that way, beside imported rows
        that say "Cafe Luna". Case is left alone: that is the user's choice of
        how a name reads, and matching ignores it anyway.
        """
        if value is None:
            return None
        tidy = " ".join(value.split())
        if not tidy:
            raise ValueError("must not be blank")
        return tidy

    @model_validator(mode="after")
    def _says_something(self) -> TransactionPatchIn:
        # An empty correction would clear the review flag while changing
        # nothing — "confirm" wearing a different verb. Confirming has its own
        # endpoint so that the two stay distinguishable in what they mean.
        sent = {
            name for name in self.model_fields_set if getattr(self, name) is not None
        }
        if not sent:
            raise ValueError("nothing to change — use confirm to accept a row as it is")
        return self


class TransactionOut(ReviewRowOut):
    needs_review: bool


class PatchOutcomeOut(BaseModel):
    transaction: TransactionOut
    # True when this was the last row from its import still waiting, so the
    # client can say "statement done" without asking again.
    import_finished: bool = False
    # Whether a category change was learned as a rule for this merchant. False
    # when the category did not change, or the row has no merchant to learn
    # from — the row is still corrected either way.
    rule_recorded: bool = False
    # How many other rows in the queue took the new category. Said out loud
    # because rows changing that the user did not touch should never be silent.
    recategorized: int = 0


class ConfirmIn(BaseModel):
    """Rows the user accepts exactly as extracted.

    Bulk because the common case is twenty right rows and two wrong ones, and
    making someone tap twenty times to say "yes" is how a review queue gets
    abandoned. Capped well above any real statement so one request cannot be
    made to lock half a table.
    """

    ids: list[uuid.UUID] = Field(min_length=1, max_length=1_000)


class ConfirmOutcomeOut(BaseModel):
    # Rows that left the queue because of this request. Confirming a row that
    # was already confirmed is not an error — the request is idempotent — but
    # it is not counted either, so a retry reports 0 rather than repeating
    # the first answer.
    confirmed: int
    # Imports whose last outstanding row this request resolved.
    imports_finished: list[uuid.UUID] = []


class DeleteOutcomeOut(BaseModel):
    # A body rather than a bare 204, so deleting the last row still waiting
    # from an import can say the import is done — the same answer PATCH and
    # confirm give, so the client handles one shape for "a row was resolved".
    import_finished: bool = False
