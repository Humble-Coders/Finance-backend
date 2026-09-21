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

from pydantic import BaseModel, Field

from app.models.enums import SourceKind, TransactionDirection
from app.services.statements import MAX_TEXT_CHARS

__all__ = ["StatementParseIn", "ParsedRowOut", "StatementParseOut"]


class StatementParseIn(BaseModel):
    source_kind: SourceKind
    page_count: int | None = Field(default=None, ge=1, le=500)
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    # Optional here; 3.3 makes it required, once accounts can be created. An
    # import filed against the wrong account breaks dedup for both of them, so
    # this is never guessed or defaulted.
    account_id: uuid.UUID | None = None
    # Opt-in, per import, and only honoured when the import actually went badly
    # (PRD F2, 2026-09-21). Defaulting to False is the safe default and is
    # asserted by a test rather than trusted.
    keep_text_for_diagnostics: bool = False


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
