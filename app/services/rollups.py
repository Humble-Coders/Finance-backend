"""Monthly totals: each household's money summed by month, category and
direction, kept current as the ledger changes (#83).

The AI coach answers questions over any period a household has imported —
"how has my grocery spending changed over ten years?" — and reading every
transaction for that is tens of thousands of rows. `monthly_category_total`
holds the same money as about a thousand, and its figures are the dashboard's:
the same duplicate rule (`not_an_unresolved_duplicate`), every currency kept
apart so a reader applies `countable`'s currency half itself.

**Recompute the months touched, never the whole history.** Every write to the
ledger calls `refresh` with the dates it changed, in the same transaction as
the write, so the totals are never seen out of step with the rows under them.
`rebuild` redoes a whole household, for the backfill script.

Each refresh also bumps `ledger_state.ledger_version`. Anything cached from
the ledger is keyed by it, so a write invalidates the cache for free.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import date

from sqlalchemy import Date, String, case, cast, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.categorization import Category
from app.models.derived import LedgerState, MonthlyCategoryTotal
from app.models.money import Transaction
from app.services.dashboard import not_an_unresolved_duplicate

__all__ = ["first_of_month", "ledger_version", "rebuild", "refresh"]


def first_of_month(day: date) -> date:
    return day.replace(day=1)


async def refresh(
    session: AsyncSession, household_id: uuid.UUID, dates: Iterable[date | None]
) -> None:
    """Rebuild the months [dates] fall in, and bump the ledger version.

    Pass every date a write touched — for a moved transaction, the old date
    and the new. Duplicates and `None` are ignored; with no dates at all it
    does nothing, and the version stays where it was.
    """
    months = sorted({first_of_month(d) for d in dates if d is not None})
    if not months:
        return
    await _bump(session, household_id)
    await session.execute(
        delete(MonthlyCategoryTotal).where(
            MonthlyCategoryTotal.household_id == household_id,
            MonthlyCategoryTotal.month.in_(months),
        )
    )
    await _insert(session, household_id, months)


async def rebuild(session: AsyncSession, household_id: uuid.UUID) -> None:
    """Rebuild every month a household has, from nothing."""
    await _bump(session, household_id)
    await session.execute(
        delete(MonthlyCategoryTotal).where(
            MonthlyCategoryTotal.household_id == household_id
        )
    )
    await _insert(session, household_id, None)


async def ledger_version(session: AsyncSession, household_id: uuid.UUID) -> int:
    """The household's ledger version: 0 before its first write."""
    result = await session.execute(
        select(LedgerState.ledger_version).where(
            LedgerState.household_id == household_id
        )
    )
    return result.scalar_one_or_none() or 0


async def _bump(session: AsyncSession, household_id: uuid.UUID) -> None:
    """Version + 1 — first, because the upsert's row lock is also what makes
    two refreshes of one household take turns.

    Without it, two requests rebuilding the same month at once would each
    delete the old rows, and the second insert would collide with the
    first's. With it, the second waits for the first to commit, and its
    delete (a new statement, so a new snapshot) sees and removes the first's
    rows.
    """
    statement = pg_insert(LedgerState).values(
        id=uuid.uuid4(), household_id=household_id, ledger_version=1
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=[LedgerState.household_id],
            set_={
                "ledger_version": LedgerState.ledger_version + 1,
                "updated_at": func.now(),
            },
        )
    )


async def _insert(
    session: AsyncSession, household_id: uuid.UUID, months: list[date] | None
) -> None:
    month = cast(func.date_trunc("month", Transaction.occurred_on), Date)
    conditions = [
        Transaction.household_id == household_id,
        not_an_unresolved_duplicate(),
    ]
    if months is not None:
        # The range lets the (household, occurred_on) index do the work; the
        # IN keeps a gap between two months out.
        last = months[-1]
        after = date(last.year + last.month // 12, last.month % 12 + 1, 1)
        conditions += [
            Transaction.occurred_on >= months[0],
            Transaction.occurred_on < after,
            month.in_(months),
        ]
    rows = (
        select(
            func.gen_random_uuid(),
            Transaction.household_id,
            Transaction.currency,
            month,
            Transaction.category_id,
            Category.slug,
            cast(Transaction.direction, String),
            func.sum(Transaction.amount_minor_units),
            func.coalesce(
                func.sum(
                    case(
                        (
                            Transaction.is_one_off.is_(True),
                            Transaction.amount_minor_units,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
            func.count(Transaction.id),
        )
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(*conditions)
        .group_by(
            Transaction.household_id,
            Transaction.currency,
            month,
            Transaction.category_id,
            Category.slug,
            Transaction.direction,
        )
    )
    await session.execute(
        MonthlyCategoryTotal.__table__.insert().from_select(
            [
                "id",
                "household_id",
                "currency",
                "month",
                "category_id",
                "category_slug",
                "direction",
                "total_minor_units",
                "one_off_minor_units",
                "transaction_count",
            ],
            rows,
        )
    )
