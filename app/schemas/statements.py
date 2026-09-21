"""Request and response shapes for statement import.

`text` is the whole reason this file needs care: it is somebody's bank
statement, redacted but still theirs. It must not appear in a validation error,
a log line or an exception message — see the validation handler in `app.main`,
which strips the offending input out of 422 responses precisely because Pydantic
helpfully puts it back in by default.
"""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel, Field, model_validator

from app.models.enums import SourceKind, TransactionDirection

__all__ = ["StatementParseIn", "ParsedRowOut", "StatementParseOut"]


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
    rows: list[ParsedRowOut]
    # How many lines the model offered that did not survive validation — most
    # often an invented amount. Surfaced, not swallowed: it is the difference
    # between "3 transactions" and "3 of the ones we could read".
    unparsed_line_count: int
    model: str
    prompt_version: str
    text_retained_until: date | None = None
