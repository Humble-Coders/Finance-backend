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

from app.core.money import MoneyError, to_minor_units
from app.models.enums import ReviewReason, TransactionDirection, TransactionSource
from app.models.money import Account, Transaction
from app.services.normalization import merchant as readable_merchant
from app.services.normalization import normalized

__all__ = [
    "RowToSave",
    "SaveOutcome",
    "RowValidationError",
    "save_rows",
    "NEAR_MATCH_DAYS",
    "collides_with",
    "ExactDuplicate",
    "save_manual",
]

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
class RowValidationError(Exception):
    """A row this endpoint cannot store, named by its position in the request.

    This is the one endpoint in the feature where a **person types the amount**
    — the rows arrive as the user corrected them on the review screen, not as
    we parsed them. So `12,40` off a French-Canadian keyboard, or `12.345` from
    a client doing its own arithmetic, is an ordinary input and not an attack.

    Unguarded, `to_minor_units` raised straight out of the request and took the
    whole import with it: one mistyped row, a 500, and every valid row beside
    it lost. Naming the row lets the client highlight the field the user is
    looking at, the same way the setup wizard does.
    """

    index: int
    field: str
    message: str


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
    for index, row in enumerate(rows):
        try:
            minor = to_minor_units(row.amount, currency)
        except MoneyError as exc:
            raise RowValidationError(index, "amount", str(exc)) from exc
        key = normalized(row.description)
        group = (row.occurred_on, minor, key)
        seen[group] += 1
        out.append((row, key, minor, seen[group]))
    return out


async def _candidates(
    session: AsyncSession,
    account_id,
    statement_import_id,
    rows: list[tuple[RowToSave, str, int, int]],
) -> dict[tuple[date, int], list[Transaction]]:
    """Every row this import could collide with, in one query.

    One query per row is the obvious shape and it degrades badly: a 200-line
    statement is 200 round trips before a single insert, and each one crosses
    the pooler. Measured at ~4ms a row against a local container, which is
    fractions of a second there and seconds through Supabase — inside a request
    somebody is waiting on.

    **Rows from this same import are excluded, and that is not an
    optimisation.** Two lines on one statement are two transactions by
    definition — the statement listed them both — so neither can be a duplicate
    of the other. Without it, two $100 e-transfers to different people on one
    day flag each other, and so does every pair of same-day $20 withdrawals.
    """
    if not rows:
        return {}

    dates = [row.occurred_on for row, _, _, _ in rows]
    amounts = {minor for _, _, minor, _ in rows}
    window = timedelta(days=NEAR_MATCH_DAYS)

    result = await session.execute(
        select(Transaction).where(
            Transaction.account_id == account_id,
            Transaction.amount_minor_units.in_(amounts),
            Transaction.occurred_on >= min(dates) - window,
            Transaction.occurred_on <= max(dates) + window,
            Transaction.statement_import_id.is_distinct_from(statement_import_id),
        )
    )

    found: dict[tuple[date, int], list[Transaction]] = {}
    for existing in result.scalars().all():
        found.setdefault(
            (existing.occurred_on, existing.amount_minor_units), []
        ).append(existing)
    return found


def _near_match(
    candidates: dict[tuple[date, int], list[Transaction]],
    occurred_on: date,
    minor: int,
    key: str,
) -> Transaction | None:
    """An earlier row that is probably this one, wearing a different name.

    Matched on account, amount and date — **never** on how similar the
    descriptions look. The case this exists for is a re-import after the parse
    improved, where the description is the single thing guaranteed to differ; a
    similarity gate would miss it for precisely the reason it changed.
    """
    for offset in range(-NEAR_MATCH_DAYS, NEAR_MATCH_DAYS + 1):
        for existing in candidates.get(
            (occurred_on + timedelta(days=offset), minor), []
        ):
            if existing.normalized_description != key:
                return existing
    return None


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
    numbered = _numbered(rows, currency)
    candidates = await _candidates(session, account_id, statement_import_id, numbered)

    for row, key, minor, occurrence in numbered:
        existing = _near_match(candidates, row.occurred_on, minor, key)
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


async def collides_with(
    session: AsyncSession,
    *,
    transaction: Transaction,
    occurred_on: date,
    amount_minor_units: int,
    normalized_description: str,
) -> Transaction | None:
    """The row an edited transaction would become a duplicate of, or None.

    Mirrors `uq_transaction_dedup` exactly — account, date, amount,
    normalized description, occurrence — because the question being asked is
    "would the database refuse this?", and any looser definition answers a
    different question than the one the insert will ask.

    **Deliberately not the near-match rule `save_rows` uses.** At import time a
    same-day same-amount row with a different description is *flagged* for a
    person to compare; refusing an edit on those grounds would block a
    perfectly ordinary correction, because two $20 withdrawals on one afternoon
    are two withdrawals. Flagging is advice and refusing is a wall, and only
    the exact key earns a wall.

    Excludes the row being edited, which would otherwise always match itself.
    """
    result = await session.execute(
        select(Transaction).where(
            Transaction.account_id == transaction.account_id,
            Transaction.occurred_on == occurred_on,
            Transaction.amount_minor_units == amount_minor_units,
            Transaction.normalized_description == normalized_description,
            Transaction.occurrence == transaction.occurrence,
            Transaction.id != transaction.id,
        )
    )
    return result.scalars().first()


def may_merge_onto(existing: Transaction) -> bool:
    """Whether a re-import may overwrite this row's description and category.

    No, once the user has touched it. We improved the parse; they told us what
    the transaction actually was, and a better guess does not outrank an answer.

    **Nothing calls this yet.** It was written for 3.4 to use in a merge — the
    review queue answering "yes, same transaction" by moving the better parse
    onto the older row — but 3.4 (#37) did not include a merge, and a suspected
    duplicate is answered today by deleting one of the two rows. Kept because
    the rule it states is the one a merge will need; see the follow-ups in
    `handoffs/ticket-37.md`.
    """
    return existing.needs_review


# How many times a manual save re-reads the next free occurrence after losing a
# race for it. Two people in one household tapping "keep both" on the same
# entry in the same instant is already unlikely; three in a row is a bug.
_OCCURRENCE_ATTEMPTS = 3


@dataclass(frozen=True)
class ExactDuplicate(Exception):
    """A typed-in entry is exactly one already recorded — same account, day,
    amount and description. `match` is the recorded one, for the person to see.
    """

    match: Transaction


async def _same_key(
    session: AsyncSession, account_id, occurred_on: date, minor: int, key: str
) -> list[Transaction]:
    """Every recorded row with this dedup key, lowest occurrence first."""
    result = await session.execute(
        select(Transaction)
        .where(
            Transaction.account_id == account_id,
            Transaction.occurred_on == occurred_on,
            Transaction.amount_minor_units == minor,
            Transaction.normalized_description == key,
        )
        .order_by(Transaction.occurrence)
    )
    return list(result.scalars().all())


async def _near_match_for_one(
    session: AsyncSession, account_id, occurred_on: date, minor: int, key: str
) -> Transaction | None:
    """The import rule for a suspected duplicate, asked about one typed row.

    Same account and amount, within `NEAR_MATCH_DAYS`, a *different*
    description — the manager chose (2026-09-29) to flag manual entries by the
    very rule imports use, so the review queue asks the same question whichever
    way a row arrived.
    """
    window = timedelta(days=NEAR_MATCH_DAYS)
    result = await session.execute(
        select(Transaction).where(
            Transaction.account_id == account_id,
            Transaction.amount_minor_units == minor,
            Transaction.occurred_on >= occurred_on - window,
            Transaction.occurred_on <= occurred_on + window,
        )
    )
    candidates: dict[tuple[date, int], list[Transaction]] = {}
    for existing in result.scalars().all():
        candidates.setdefault(
            (existing.occurred_on, existing.amount_minor_units), []
        ).append(existing)
    return _near_match(candidates, occurred_on, minor, key)


async def save_manual(
    session: AsyncSession,
    *,
    household_id,
    account: Account,
    occurred_on: date,
    amount: str,
    direction: TransactionDirection,
    description: str,
    category_id=None,
    allow_duplicate: bool = False,
) -> Transaction:
    """Write one transaction a person typed in (#38). Nothing is committed.

    **An exact copy is refused, unless the person said it is real.** Someone
    typing in a coffee they already imported should be told, not quietly given
    two — so the same account, day, amount and description raises
    `ExactDuplicate` naming the recorded row. With `allow_duplicate` the entry
    is saved as the *next occurrence* of that key instead: two identical
    coffees on one day are two coffees, and `occurrence` exists to say so.

    The database still has the last word. The check above and the insert race
    a second request doing the same thing, so the insert does nothing on a
    collision and the answer is read back from what is actually there.

    Raises `MoneyError` for an amount the account's currency cannot hold.
    """
    minor = to_minor_units(amount, account.currency)
    key = normalized(description)

    for _ in range(_OCCURRENCE_ATTEMPTS):
        same = await _same_key(session, account.id, occurred_on, minor, key)
        if same and not allow_duplicate:
            raise ExactDuplicate(same[0])
        occurrence = max((row.occurrence for row in same), default=0) + 1

        existing = await _near_match_for_one(
            session, account.id, occurred_on, minor, key
        )
        inserted = (
            await session.execute(
                pg_insert(Transaction)
                .values(
                    household_id=household_id,
                    account_id=account.id,
                    statement_import_id=None,
                    occurred_on=occurred_on,
                    amount_minor_units=minor,
                    currency=account.currency,
                    direction=direction,
                    description=description,
                    normalized_description=key,
                    merchant=readable_merchant(description),
                    occurrence=occurrence,
                    source=TransactionSource.manual,
                    category_id=category_id,
                    needs_review=existing is not None,
                    review_reason=(
                        ReviewReason.suspected_duplicate if existing else None
                    ),
                    duplicate_of_id=existing.id if existing else None,
                )
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
        ).scalar_one_or_none()
        if inserted is not None:
            log.info(
                "manual_transaction_saved",
                transaction_id=str(inserted),
                occurrence=occurrence,
                flagged=existing is not None,
            )
            return await session.get(Transaction, inserted)

    # Lost the race every time: somebody else keeps taking the next number.
    same = await _same_key(session, account.id, occurred_on, minor, key)
    if same:
        raise ExactDuplicate(same[0])
    raise RuntimeError("could not find a free occurrence for a manual transaction")
