"""The review queue: what is still waiting for a person, and what that means.

Rows arrive here from extraction (3.3) carrying `needs_review` and a
[ReviewReason]. This module owns the two questions that outlive any single
endpoint: how the queue is paged, and when an import counts as finished.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.money import StatementImport, Transaction

__all__ = ["Cursor", "CursorError", "PAGE_SIZE", "stamp_if_finished"]

PAGE_SIZE = 50


class CursorError(ValueError):
    """The cursor was not one we issued."""


@dataclass(frozen=True)
class Cursor:
    """Where the last page stopped.

    Keyset, not offset. An offset re-counts rows each page, so a row confirmed
    between two requests shifts everything after it up by one and the reader
    never sees the row that slid past the boundary — in a queue whose whole
    purpose is that every row gets looked at, silently skipping one is the
    worst thing paging can do.

    Carries `id` as well as the date because many transactions share a date:
    ordering on the date alone leaves ties in an order the database is free to
    change between queries, which reintroduces the skipping it was meant to
    prevent.
    """

    occurred_on: date
    transaction_id: uuid.UUID

    def encode(self) -> str:
        raw = f"{self.occurred_on.isoformat()}:{self.transaction_id}"
        return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str) -> Cursor:
        try:
            padded = value + "=" * (-len(value) % 4)
            raw = base64.urlsafe_b64decode(padded.encode()).decode()
            stamp, _, identifier = raw.partition(":")
            return cls(
                occurred_on=date.fromisoformat(stamp),
                transaction_id=uuid.UUID(identifier),
            )
        except (ValueError, binascii.Error, UnicodeDecodeError) as error:
            # Never a 500: a cursor is client-supplied, and a stale or
            # hand-edited one is a bad request, not a server fault.
            raise CursorError("cursor is not readable") from error


async def stamp_if_finished(
    session: AsyncSession, record: StatementImport | None
) -> int:
    """How many rows from this import are still in the queue; stamps at zero.

    `confirmed_at` means "the user has finished with this import". With no
    document held anywhere, this stamp is the only record that a person looked
    at what was extracted — so it is set exactly when the last flagged row is
    resolved, and never while one is outstanding.

    Takes the record rather than its id on purpose: every caller already holds
    it, and re-fetching inside here bought nothing but a second query on a
    connection the caller owns.

    Returns the outstanding count so a caller can both report it and rely on
    one definition of finished.
    """
    if record is None:
        return 0

    outstanding = await session.execute(
        select(func.count())
        .select_from(Transaction)
        .where(
            Transaction.statement_import_id == record.id,
            Transaction.needs_review.is_(True),
        )
    )
    remaining = int(outstanding.scalar_one())
    if remaining == 0 and record.confirmed_at is None:
        record.confirmed_at = datetime.now(UTC)
    return remaining
