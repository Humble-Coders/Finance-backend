"""Request and response shapes for statement import.

`text` is the whole reason this file needs care: it is somebody's bank
statement, redacted but still theirs. It must not appear in a validation error,
a log line or an exception message — see the validation handler in `app.main`,
which strips the offending input out of 422 responses precisely because Pydantic
helpfully puts it back in by default.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.enums import SourceKind, StatementImportStatus, TransactionDirection

# Ten years. Long enough for anyone importing historical statements, short
# enough that a typo'd year lands outside it rather than in the ledger.
OLDEST_IMPORTABLE_DAYS = 3_650

__all__ = [
    "StatementParseIn",
    "ParsedRowOut",
    "StatementParseOut",
    "ConfirmRowIn",
    "ConfirmRowsIn",
    "SaveOutcomeOut",
    "StatementImportOut",
]


class StatementParseIn(BaseModel):
    source_kind: SourceKind
    page_count: int | None = Field(default=None, ge=1, le=500)
    # No max_length here on purpose: Pydantic would answer 422, and the
    # ticket asks for 413 on an oversized statement. The endpoint checks it.
    text: str = Field(min_length=1)
    # Optional here; 3.3 makes it required, once accounts can be created. An
    # import filed against the wrong account breaks dedup for both of them, so
    # this is never guessed or defaulted.
    account_id: uuid.UUID | None = None
    # When the statement ran, sent as a field rather than left in the text.
    # Statements print the year once, in a header block that the on-device
    # redactor deliberately drops — it is where the name and address live. So
    # the device reads the period *before* it redacts and sends it here. A date
    # range is not personal data, and without it every date on the statement is
    # a day and a month with no year, which the model is told to refuse rather
    # than guess at.
    statement_period_start: date | None = None
    statement_period_end: date | None = None
    # Opt-in, per import, and only honoured when the import actually went badly
    # (PRD F2, 2026-09-21). Defaulting to False is the safe default and is
    # asserted by a test rather than trusted.
    keep_text_for_diagnostics: bool = False

    @model_validator(mode="after")
    def _period_is_a_period(self) -> StatementParseIn:
        start, end = self.statement_period_start, self.statement_period_end
        if (start is None) != (end is None):
            raise ValueError("a statement period needs both ends or neither")
        if start is not None and end is not None and end < start:
            raise ValueError("a statement period cannot end before it starts")
        return self


class ParsedRowOut(BaseModel):
    occurred_on: date
    description: str
    # Decimal string, per the money boundary (PRD §4.4).
    amount: str
    direction: TransactionDirection
    confidence: int


class StatementParseOut(BaseModel):
    import_id: uuid.UUID
    # The amounts above are decimal strings in this currency. Sent explicitly
    # rather than left for the client to infer from /capabilities: PRD §4.4
    # carries money as an amount *and* a currency, and a client formatting
    # "12.40" with the wrong symbol is the kind of error nobody reports and
    # everybody notices.
    currency: str
    rows: list[ParsedRowOut]
    # How many lines the model offered that did not survive validation — most
    # often an invented amount. Surfaced, not swallowed: it is the difference
    # between "3 transactions" and "3 of the ones we could read".
    unparsed_line_count: int
    model: str
    prompt_version: str
    text_retained_until: date | None = None


class ConfirmRowIn(BaseModel):
    """A row the user is saving, as they confirmed it — not as we parsed it.

    They may have corrected the date, the amount or the description on the
    review screen before pressing save, so this is the record of what they
    said, not an echo of what the model read.
    """

    occurred_on: date
    description: str = Field(min_length=1, max_length=512)
    # Format is checked in the service, where the household's currency is
    # known; the sign is checked here, because it needs no currency and means
    # the same thing everywhere.
    amount: str
    direction: TransactionDirection
    confidence: int = Field(default=100, ge=0, le=100)

    @field_validator("amount")
    @classmethod
    def _not_negative(cls, value: str) -> str:
        """`direction` carries the sign; the amount must not carry it too.

        `-50.00` with `direction=debit` is ambiguous by construction — money
        out, or a refund? Nothing downstream can tell, and the parse path only
        ever produces positives. One typed minus would put a figure in the
        ledger whose meaning depends on who reads it. Ticket #38 settled this
        for manually typed transactions; this endpoint takes typed rows too.
        """
        if value.strip().startswith("-"):
            raise ValueError(
                "must not be negative — use direction to say which way it went"
            )
        return value

    @field_validator("occurred_on")
    @classmethod
    def _within_living_memory(cls, value: date) -> date:
        """A date the rest of the product can reason about.

        Every period the product is built on keys off this: M4's budgets,
        "spending this month", the health score's windows. A row dated 2999
        sits outside all of them forever, and outside the review queue too,
        because nothing flags a date nobody checked. A day of tolerance ahead
        covers a timezone edge without admitting a typo'd year.
        """
        today = date.today()
        if value > today + timedelta(days=1):
            raise ValueError("cannot be in the future")
        if value < today - timedelta(days=OLDEST_IMPORTABLE_DAYS):
            raise ValueError("is too far in the past to be a statement line")
        return value


class ConfirmRowsIn(BaseModel):
    account_id: uuid.UUID
    rows: list[ConfirmRowIn] = Field(min_length=1, max_length=2_000)


class SaveOutcomeOut(BaseModel):
    import_id: uuid.UUID
    saved: int
    # Exact matches the database refused. Certain, so not presented as work.
    duplicates: int
    # Saved, but pointed at something they might be a second copy of.
    flagged: int
    needs_review: int


class StatementImportOut(BaseModel):
    id: uuid.UUID
    status: StatementImportStatus
    source_kind: SourceKind
    page_count: int | None
    extracted_count: int | None
    saved: int
    needs_review: int
    confirmed_at: datetime | None
