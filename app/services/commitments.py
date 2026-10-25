"""The month's commitments: what setup said is due, and what was seen paid.

One place for two readers. Home ticks commitments off (`dashboard.build`) and
the budget shows the same checklist and counts what is still to come, so both
must match payments the same way — a commitment Home calls paid cannot be
"still expected" on the next tab.

**A commitment's category** is the budget line it belongs to. Set in the
wizard; for a row saved before the wizard asked, the preset names below stand
in, and anything else has none — it is still listed and still counted, it
just holds no line up.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.categorization import Category
from app.models.enums import TransactionDirection
from app.models.money import Transaction
from app.models.setup import Obligation

# The shared categories a commitment can belong to: spending ones. Not income
# or transfers (not spending), not savings (the budget's remainder), and not
# debt payments — debts are entered as debts, whose minimums hold the Debt line.
COMMITMENT_SLUGS = frozenset(
    {
        "rent",
        "utilities",
        "insurance",
        "subscriptions",
        "transport",
        "groceries",
        "dining",
        "education",
        "entertainment",
        "fees",
        "gifts_donations",
        "healthcare",
        "other",
        "personal_care",
        "shopping",
        "travel",
    }
)

# The wizard's presets (mobile `SetupWizard.COMMITMENT_PRESETS`, English
# names) and the line each belongs to — only for rows saved without one.
PRESET_CATEGORIES: dict[str, str] = {
    "rent": "rent",
    "mortgage": "rent",
    "phone": "utilities",
    "internet": "utilities",
    "hydro": "utilities",
    "car insurance": "insurance",
    "home insurance": "insurance",
    "childcare": "other",
    "gym": "subscriptions",
    "streaming": "subscriptions",
    "transit pass": "transport",
}


@dataclass(frozen=True)
class Match:
    """The transaction a commitment was paid by, as far as we can tell."""

    transaction_id: uuid.UUID
    occurred_on: date
    amount_minor_units: int
    description: str | None


@dataclass(frozen=True)
class Commitment:
    """One obligation, and whether a payment for it turned up this month.

    [match] being None means **not seen**, which is not the same as not paid:
    a commitment settled in cash, from another account, or under a name the
    statement writes differently will not be found. The wording the client
    shows has to say "not seen this month" and never "unpaid".
    """

    name: str
    expected_minor_units: int
    match: Match | None = None
    due_day: int | None = None
    # The shared category it belongs to; None when it has none.
    category_id: uuid.UUID | None = None
    category_slug: str | None = None


async def shared_categories(session: AsyncSession) -> dict[str, uuid.UUID]:
    """The shared categories a commitment can name, slug -> id."""
    result = await session.execute(
        select(Category.slug, Category.id).where(
            Category.household_id.is_(None), Category.slug.in_(COMMITMENT_SLUGS)
        )
    )
    return dict(result.all())


def slug_for(obligation: Obligation, slug_by_id: dict[uuid.UUID, str]) -> str | None:
    """The category slug [obligation] belongs to, or None."""
    if obligation.category_id is not None:
        return slug_by_id.get(obligation.category_id)
    return PRESET_CATEGORIES.get(obligation.name.strip().lower())


async def obligations(
    session: AsyncSession, household_id: uuid.UUID, currency: str
) -> list[Obligation]:
    return list(
        (
            await session.execute(
                select(Obligation)
                .where(
                    Obligation.household_id == household_id,
                    Obligation.currency == currency,
                )
                .order_by(Obligation.position, Obligation.name)
            )
        )
        .scalars()
        .all()
    )


async def for_month(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    first: date,
    last: date,
) -> list[Commitment]:
    """Every commitment, each with the payment that settles it in the month."""
    # Imported here: the dashboard imports this module.
    from app.services.dashboard import countable, match_commitment

    # Every debit in the month, once, and then matched in memory. The
    # alternative is a query per obligation, which is a query per row of a list
    # the user controls the length of.
    debits = list(
        (
            await session.execute(
                select(Transaction).where(
                    *countable(household_id, currency),
                    Transaction.occurred_on >= first,
                    Transaction.occurred_on <= last,
                    Transaction.direction == TransactionDirection.debit,
                )
            )
        )
        .scalars()
        .all()
    )
    rows = await obligations(session, household_id, currency)
    shared = await shared_categories(session)
    slug_by_id = {id_: slug for slug, id_ in shared.items()}

    # A transaction settles at most one commitment. Without this, two
    # commitments of a similar size both claim the same payment and the
    # checklist says two things were paid when one was.
    claimed: set[uuid.UUID] = set()
    out: list[Commitment] = []
    for obligation in rows:
        available = [row for row in debits if row.id not in claimed]
        found = match_commitment(
            obligation.name, obligation.monthly_amount_minor_units, available
        )
        if found is not None:
            claimed.add(found.id)
        slug = slug_for(obligation, slug_by_id)
        out.append(
            Commitment(
                name=obligation.name,
                expected_minor_units=obligation.monthly_amount_minor_units,
                due_day=obligation.due_day,
                category_id=shared.get(slug) if slug else None,
                category_slug=slug,
                match=(
                    Match(
                        transaction_id=found.id,
                        occurred_on=found.occurred_on,
                        amount_minor_units=found.amount_minor_units,
                        description=found.description,
                    )
                    if found is not None
                    else None
                ),
            )
        )
    return out


async def floors(
    session: AsyncSession, household_id: uuid.UUID, currency: str
) -> dict[uuid.UUID, int]:
    """Category id -> the monthly commitments in it: the least its line holds."""
    shared = await shared_categories(session)
    slug_by_id = {id_: slug for slug, id_ in shared.items()}
    out: dict[uuid.UUID, int] = {}
    for obligation in await obligations(session, household_id, currency):
        slug = slug_for(obligation, slug_by_id)
        if slug is None or slug not in shared:
            continue
        key = shared[slug]
        out[key] = out.get(key, 0) + obligation.monthly_amount_minor_units
    return out
