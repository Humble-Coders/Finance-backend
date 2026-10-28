"""Ten years of synthetic money for one household: 2016-01 to 2025-12 (#83).

About 36,000 transactions — 300 a month — across several categories and
merchants, with what a long history really holds: a pay cheque twice a month,
rent, a transfer to savings, a one-off laptop every January, rows not filed
yet, rows in another currency, and unresolved suspected duplicates. A seeded
generator, so every run is the same data and a failure can be reproduced.

Inserted straight into the ledger (not through the API: 36,000 requests would
take minutes), then the monthly totals are rebuilt from it, as the backfill
script does in production.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import insert, select

from app.models.categorization import Category
from app.models.enums import ReviewReason, TransactionDirection, TransactionSource
from app.models.money import Transaction
from app.services import rollups

FIRST = date(2016, 1, 1)
LAST = date(2025, 12, 1)
MONTHS = [date(year, month, 1) for year in range(2016, 2026) for month in range(1, 13)]

# Everyday spending: category -> merchants, with how often each category comes up.
SPENDING = {
    "groceries": (["LOBLAWS", "METRO", "NO FRILLS", "COSTCO"], 30),
    "dining": (["TIM HORTONS", "STARBUCKS", "PIZZA PIZZA", "SUSHI BAR"], 25),
    "shopping": (["AMAZON MARKETPLACE", "WINNERS", "CANADIAN TIRE"], 20),
    "transport": (["PRESTO", "SHELL", "UBER"], 15),
    "utilities": (["HYDRO ONE", "ROGERS"], 5),
    "subscriptions": (["NETFLIX", "SPOTIFY"], 5),
}
EVERYDAY_A_MONTH = 287
COMMON_MERCHANT = "AMAZON MARKETPLACE"


@dataclass(frozen=True)
class LongHistory:
    household_id: uuid.UUID
    account_id: uuid.UUID
    currency: str
    rows: int


async def ten_years(
    db_session, household_id: uuid.UUID, account_id: uuid.UUID, currency: str = "CAD"
) -> LongHistory:
    result = await db_session.execute(
        select(Category.slug, Category.id).where(Category.household_id.is_(None))
    )
    category = dict(result.all())
    rng = random.Random(83)
    rows: list[dict] = []

    def add(
        month: date,
        merchant: str,
        cents: int,
        *,
        slug: str | None,
        credit: bool = False,
        one_off: bool = False,
        in_currency: str = currency,
        duplicate: bool = False,
        day: int | None = None,
    ) -> None:
        rows.append(
            {
                "id": uuid.uuid4(),
                "household_id": household_id,
                "account_id": account_id,
                "occurred_on": month.replace(day=day or rng.randint(1, 28)),
                "amount_minor_units": cents,
                "currency": in_currency,
                "direction": (
                    TransactionDirection.credit
                    if credit
                    else TransactionDirection.debit
                ),
                "description": merchant,
                "normalized_description": merchant.lower(),
                "merchant": merchant,
                "source": TransactionSource.upload,
                # Unique per row, so the dedup index never folds two together.
                "occurrence": len(rows) + 1,
                "category_id": category[slug] if slug else None,
                "is_one_off": one_off,
                "needs_review": duplicate,
                "review_reason": ReviewReason.suspected_duplicate
                if duplicate
                else None,
            }
        )

    slugs = list(SPENDING)
    weights = [SPENDING[slug][1] for slug in slugs]
    for month in MONTHS:
        add(month, "PAYROLL", 300_000, slug="income", credit=True, day=1)
        add(month, "PAYROLL", 300_000, slug="income", credit=True, day=15)
        add(month, "LANDLORD", 180_000, slug="rent", day=1)
        add(month, "TRANSFER TO SAVINGS", 50_000, slug="transfers", day=2)
        for _ in range(EVERYDAY_A_MONTH):
            slug = rng.choices(slugs, weights)[0]
            add(
                month,
                rng.choice(SPENDING[slug][0]),
                rng.randint(300, 25_000),
                slug=slug,
            )
        # Not filed yet: still spending, counted under "uncategorized".
        add(month, "SQ *MARKET STALL", rng.randint(500, 3_000), slug=None)
        add(month, "POS 4412 PURCHASE", rng.randint(500, 3_000), slug=None)
        # Another currency: in no total.
        add(month, "AMAZON.COM", 9_999, slug="shopping", in_currency="USD")
        add(month, "SEATTLE COFFEE", 1_234, slug="dining", in_currency="USD")
        # An unresolved suspected duplicate: in no total until answered.
        add(month, "LOBLAWS", 77_777, slug="groceries", duplicate=True)
        if month.month == 1:
            add(month, "BEST BUY LAPTOP", 150_000, slug="shopping", one_off=True)

    for start in range(0, len(rows), 5_000):
        await db_session.execute(insert(Transaction), rows[start : start + 5_000])
    await rollups.rebuild(db_session, household_id)
    await db_session.flush()
    return LongHistory(household_id, account_id, currency, len(rows))
