"""Request and response shapes for statement import.

`text` is the whole reason this file needs care: it is somebody's bank
statement, redacted but still theirs. It must not appear in a validation error,
a log line or an exception message — see the validation handler in `app.main`,
which strips the offending input out of 422 responses precisely because Pydantic
helpfully puts it back in by default.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

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
    "amount_not_negative",
    "date_within_living_memory",
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
    # The category the household's rules or the model suggest, so the person
    # sees and can change it before anything is saved. None where neither
    # knows ("No category yet").
    category_id: uuid.UUID | None = None


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


def amount_not_negative(value: str) -> str:
    """`direction` carries the sign; the amount must not carry it too.

    `-50.00` with `direction=debit` is ambiguous by construction — money
    out, or a refund? Nothing downstream can tell, and the parse path only
    ever produces positives. One typed minus would put a figure in the
    ledger whose meaning depends on who reads it. Ticket #38 settled this
    for manually typed transactions; every endpoint that takes a typed
    amount shares this one rule rather than restating it.
    """
    if value.strip().startswith("-"):
        raise ValueError(
            "must not be negative — use direction to say which way it went"
        )
    return value


def date_within_living_memory(value: date) -> date:
    """A date the rest of the product can reason about.

    Every period the product is built on keys off this: M4's budgets,
    "spending this month", the health score's windows. A row dated 2999
    sits outside all of them forever, and outside the review queue too,
    because nothing flags a date nobody checked. A day of tolerance ahead
    covers a timezone edge without admitting a typo'd year.
    """
    # `datetime.now(UTC).date()`, not `date.today()`: the latter reads the
    # server's local timezone, so the boundary would depend on where the
    # code runs. It is currently correct because Render happens to be UTC —
    # correct by deployment accident, and the one-day tolerance would hide
    # the difference until it did not. Everything else in this feature
    # already uses UTC explicitly.
    today = datetime.now(UTC).date()
    if value > today + timedelta(days=1):
        raise ValueError("cannot be in the future")
    if value < today - timedelta(days=OLDEST_IMPORTABLE_DAYS):
        # Shared by imports, corrections and typed-in entries (#38), so it names
        # no one of them.
        raise ValueError("is too far in the past")
    return value


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
    # The category to save the row under, as the person left it while
    # checking. With `suggested_category_id` — what the parse suggested — the
    # server can tell a category the person chose from one they accepted: a
    # chosen one teaches a rule for the merchant, as fixing a saved row does.
    category_id: uuid.UUID | None = None
    suggested_category_id: uuid.UUID | None = None

    @field_validator("amount")
    @classmethod
    def _not_negative(cls, value: str) -> str:
        return amount_not_negative(value)

    @field_validator("occurred_on")
    @classmethod
    def _within_living_memory(cls, value: date) -> date:
        return date_within_living_memory(value)


class ConfirmRowsIn(BaseModel):
    account_id: uuid.UUID
    rows: list[ConfirmRowIn] = Field(min_length=1, max_length=2_000)
    # True from apps that show categories before saving: the rows were
    # categorised when the statement was read, so one left without a category
    # goes to review rather than back to the model. Older apps send nothing and
    # keep the old behaviour — every row categorised on save.
    categorised: bool = False
    # The balance the statement closed at and its date, as the person
    # confirmed them on the check screen (#76). The phone reads them before
    # redacting — they sit in the summary block the redactor removes — and the
    # person corrects them if need be. A debt linked to this account takes the
    # balance when it is newer than the one it has. Both or neither.
    closing_balance: str | None = None
    closing_date: date | None = None

    @field_validator("closing_date")
    @classmethod
    def _closing_within_living_memory(cls, value: date | None) -> date | None:
        return None if value is None else date_within_living_memory(value)


class SaveOutcomeOut(BaseModel):
    import_id: uuid.UUID
    saved: int
    # Exact matches the database refused. Certain, so not presented as work.
    duplicates: int
    # Saved, but pointed at something they might be a second copy of.
    flagged: int
    needs_review: int
    # Rows from earlier imports this save sent back to review: the other half
    # of a card payment or a savings move it paired (#73). Not in
    # `needs_review`, which is this import's own rows.
    earlier_needs_review: int = 0
    # Debts linked to this account whose balance the statement updated (#76).
    debts_updated: int = 0


class StatementImportOut(BaseModel):
    id: uuid.UUID
    status: StatementImportStatus
    source_kind: SourceKind
    page_count: int | None
    extracted_count: int | None
    saved: int
    needs_review: int
    confirmed_at: datetime | None
    # When it was imported. A list of statements is unreadable without it —
    # "pdf_text, 24 rows" three times over names nothing a person can pick from.
    created_at: datetime


class StatementImportsOut(BaseModel):
    """Every import this household has made, newest first.

    No cursor. The free tier allows one import a month, so a household's whole
    history is a short list for years; `limit` is the bound, and a paging
    protocol here would be ceremony around a list that does not page.
    """

    imports: list[StatementImportOut]
