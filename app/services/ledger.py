"""Saving parsed rows, and deciding which of them we have seen before.

The whole feature rests on one distinction that the rows themselves cannot
make. Two $5.25 coffees at the same shop on the same day are byte-identical to
one coffee imported twice. What separates them is **context**: two identical
lines *within* one statement are two purchases; the same line appearing in a
*later* import is a duplicate.

`occurrence` is how that context is written down. Each row is numbered within
its own import — the 1st $5.25 Tim Hortons of that day, the 2nd — and the
number is part of the unique key. Re-import the same statement and the same
numbers come out, so every row collides and nothing is written twice. A genuine
third coffee appears in a later statement as the 3rd of its group, does not
collide, and is saved.

This works because a statement is cumulative: any statement covering a day
lists every transaction of that day. Numbering within the import is therefore
stable across imports of overlapping periods, which is exactly the case that
breaks naive dedup.

Two layers, deliberately different in severity:

* **Exact** — same account, date, amount, description and occurrence. The
  database refuses it. Silent, because it is certain.
* **Near** — same account, amount, within three days, *different* description.
  Flagged for a person, never dropped. This is what catches a re-import after
  the parser improved, where the description is the one thing guaranteed to
  have changed — which is why description similarity plays no part in finding
  it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.money import to_minor_units
from app.models.enums import ReviewReason, TransactionDirection, TransactionSource
from app.models.money import Transaction
from app.services.normalization import merchant as readable_merchant
from app.services.normalization import normalized

__all__ = ["RowToSave", "SaveOutcome", "save_rows", "NEAR_MATCH_DAYS"]

log = structlog.get_logger()

# Same day only (manager decision, 2026-09-22). A wider window catches the case
# where a re-parse moves a date — a purchase and its posting can sit a couple of
# days apart — but it also flags genuinely different spending that happens to
# cost the same that week, and every false flag is work for a person.
#
# The accepted cost: if a re-import shifts a transaction's date, the near-match
# check will not see it and the row lands twice. Widen this one constant if
# review-queue volume ever shows that happening.
NEAR_MATCH_DAYS = 0

# Below this, a person should look at the row before it counts as their money.
LOW_CONFIDENCE = 70


@dataclass(frozen=True)
class RowToSave:
    occurred_on: date
    description: str
    amount: str
    direction: TransactionDirection
    confidence: int


@dataclass
class SaveOutcome:
    saved: int = 0
    # Exact matches the database refused. Certain, so not surfaced as work.
    duplicates: int = 0
    # Near matches: saved, flagged, and pointed at what they collided with.
    flagged: int = 0
    saved_ids: list = field(default_factory=list)


def _numbered(
    rows: list[RowToSave], currency: str
) -> list[tuple[RowToSave, str, int, int]]:
    """Each row with its key parts and its occurrence within this import."""
    seen: Counter[tuple[date, int, str]] = Counter()
    out = []
    for row in rows:
        minor = to_minor_units(row.amount, currency)
        key = normalized(row.description)
        group = (row.occurred_on, minor, key)
        seen[group] += 1
        out.append((row, key, minor, seen[group]))
    return out


async def _near_match(
    session: AsyncSession,
    account_id,
    statement_import_id,
    occurred_on: date,
    minor: int,
    key: str,
) -> Transaction | None:
    """An earlier row that is probably this one, wearing a different name.

    Matched on account, amount and date — **never** on how similar the
    descriptions look. The case this exists for is a re-import after the parse
    improved, where the description is the single thing guaranteed to differ; a
    similarity gate would miss it for precisely the reason it changed.

    **Rows from this same import are excluded, and that is not an optimisation.**
    Two lines on one statement are two transactions by definition — the
    statement listed them both — so neither can be a duplicate of the other.
    Without this, two $100 e-transfers to different people on one day flag each
    other, and so does every pair of same-day $20 withdrawals. A review queue
    that is mostly false alarms is a review queue people learn to ignore.
    """
    result = await session.execute(
        select(Transaction)
        .where(
            Transaction.account_id == account_id,
            Transaction.amount_minor_units == minor,
            Transaction.occurred_on >= occurred_on - timedelta(days=NEAR_MATCH_DAYS),
            Transaction.occurred_on <= occurred_on + timedelta(days=NEAR_MATCH_DAYS),
            Transaction.normalized_description != key,
            Transaction.statement_import_id.is_distinct_from(statement_import_id),
        )
        .order_by(Transaction.occurred_on)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def save_rows(
    session: AsyncSession,
    *,
    household_id,
    account_id,
    statement_import_id,
    currency: str,
    rows: list[RowToSave],
) -> SaveOutcome:
    """Write what the user confirmed, and say what was already there.

    Nothing is committed here — the caller owns the transaction, so an import
    lands whole or not at all.
    """
    outcome = SaveOutcome()

    for row, key, minor, occurrence in _numbered(rows, currency):
        existing = await _near_match(
            session, account_id, statement_import_id, row.occurred_on, minor, key
        )
        flagged = existing is not None
        low = row.confidence < LOW_CONFIDENCE

        statement = (
            pg_insert(Transaction)
            .values(
                household_id=household_id,
                account_id=account_id,
                statement_import_id=statement_import_id,
                occurred_on=row.occurred_on,
                amount_minor_units=minor,
                currency=currency,
                direction=row.direction,
                description=row.description,
                normalized_description=key,
                merchant=readable_merchant(row.description),
                occurrence=occurrence,
                source=TransactionSource.upload,
                extraction_confidence=row.confidence,
                needs_review=flagged or low,
                review_reason=(
                    ReviewReason.suspected_duplicate
                    if flagged
                    else ReviewReason.low_confidence
                    if low
                    else None
                ),
                duplicate_of_id=existing.id if existing else None,
            )
            # The database is the dedup, not this loop: two imports running at
            # once would both pass any check we made here and both insert.
            .on_conflict_do_nothing(
                index_elements=[
                    "account_id",
                    "occurred_on",
                    "amount_minor_units",
                    "normalized_description",
                    "occurrence",
                ]
            )
            .returning(Transaction.id)
        )
        inserted = (await session.execute(statement)).scalar_one_or_none()

        if inserted is None:
            outcome.duplicates += 1
            continue
        outcome.saved += 1
        outcome.saved_ids.append(inserted)
        if flagged:
            outcome.flagged += 1

    log.info(
        "statement_rows_saved",
        statement_import_id=str(statement_import_id),
        saved=outcome.saved,
        duplicates=outcome.duplicates,
        flagged=outcome.flagged,
    )
    return outcome


def may_merge_onto(existing: Transaction) -> bool:
    """Whether a re-import may overwrite this row's description and category.

    No, once the user has touched it. We improved the parse; they told us what
    the transaction actually was, and a better guess does not outrank an answer.
    Used by 3.4, which owns the merge itself.
    """
    return existing.needs_review
