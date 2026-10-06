"""A month's budget, generated from what the household actually spent (PRD F4).

Two halves, kept apart on purpose:

* **`suggest`** is the arithmetic. Integers in, lines out, no database — so
  every rule below is table-tested without one.
* **`budget_for`** and the line edits are the persistence: read the inputs,
  call `suggest`, and reconcile the result with what the user already chose.

The rules (manager decisions, 2026-10-04, ticket #56):

* A category's suggestion is the **median** of its monthly debits over the
  last three complete months before the budget's month, **a month with no
  spending counting as zero** — which is what keeps a one-off out: seen in one
  month of three, its median is zero and it gets no line. Only months from the
  household's first countable row onward are in the window, so a household
  with one month of history is budgeted from that month, not from two months
  of nothing.
* Rounded **up** to a whole currency unit: $412.37 of spending suggests $413.
* `income` and `transfers` are not spending and are never a line.
* **Debt** (`debt_payment`) is the larger of the observed median and the sum
  of the debts' minimum payments.
* **Savings** is whatever expected income has left after every other line.
  When nothing is left there is no savings line, and the budget reports the
  **shortfall** instead.

"Spent" is counted by `countable`, the dashboard's rule, so a line can never
disagree with the dashboard figure beside it.

**Regeneration.** There is no worker, so a budget is (re)generated when it is
read. A month still running is recomputed on every read: lines the user set
keep their amount and only their suggestion moves. A month that has ended
keeps the budget it had once that budget was built from some history; one
read before its statements were imported fills in when they are.

**Still learning.** Nothing is generated until the household passes the
threshold in `app/services/learning.py`, but lines set by hand are kept and
shown: manual budgeting is always available.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.money import round_up_to_whole_unit
from app.models.categorization import Category
from app.models.enums import TransactionDirection
from app.models.money import Transaction
from app.models.planning import Budget, BudgetLine, Debt
from app.models.setup import FinancialProfile
from app.services.dashboard import (
    DEBT_PAYMENT_SLUG,
    SAVINGS_SLUG,
    countable,
    month_bounds,
    months_back,
)

WINDOW_MONTHS = 3

# Money that moves but is not spent. Never a line, generated or set by hand.
NOT_BUDGETABLE = frozenset({"income", "transfers"})

# Allocations with rules of their own rather than a median of spending.
DEDICATED = frozenset({DEBT_PAYMENT_SLUG, SAVINGS_SLUG})


# --- The arithmetic -----------------------------------------------------------


@dataclass(frozen=True)
class CategoryRef:
    """A category as `suggest` needs it: which line, and what kind."""

    id: uuid.UUID
    slug: str


@dataclass(frozen=True)
class Suggestion:
    # Spending lines only, category id -> minor units. Never zero.
    spending: dict[uuid.UUID, int]
    # The dedicated lines; 0 means no line.
    debt: int
    savings: int
    # How far the lines exceed expected income; None when they do not.
    shortfall: int | None


def median(values: Sequence[int]) -> Decimal:
    """The middle value; between the middle two for an even count.

    Decimal because the mean of two minor-unit integers can fall on half a
    unit, and that half must survive until the one rounding step.
    """
    if not values:
        return Decimal(0)
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return Decimal(ordered[middle])
    return (Decimal(ordered[middle - 1]) + Decimal(ordered[middle])) / 2


def savings_and_shortfall(
    expected_income: int, other_lines: int
) -> tuple[int, int | None]:
    """(savings, shortfall) once every non-savings line is [other_lines].

    Exactly balanced is neither: no savings line, and nothing short.
    """
    remainder = expected_income - other_lines
    if remainder > 0:
        return remainder, None
    return 0, (-remainder if remainder < 0 else None)


def suggest(
    monthly_totals_by_category: Mapping[CategoryRef, Sequence[int]],
    expected_income: int,
    minimum_payments: int,
    currency: str,
) -> Suggestion:
    """The generated budget, from each category's monthly debit totals.

    Each sequence holds one total per month in the window, zeros included —
    the zeros are what filter one-offs. Every sequence is the same window.
    """
    spending: dict[uuid.UUID, int] = {}
    debt_months: list[int] | None = None
    for category, totals in monthly_totals_by_category.items():
        if category.slug in NOT_BUDGETABLE or category.slug == SAVINGS_SLUG:
            continue
        if category.slug == DEBT_PAYMENT_SLUG:
            # A household's own `debt_payment` folds into the one debt line,
            # as the dashboard folds it into one debt figure.
            debt_months = (
                list(totals)
                if debt_months is None
                else [a + b for a, b in zip(debt_months, totals, strict=True)]
            )
            continue
        amount = round_up_to_whole_unit(median(totals), currency)
        if amount > 0:
            spending[category.id] = amount

    observed_debt = round_up_to_whole_unit(median(debt_months or []), currency)
    debt = max(observed_debt, minimum_payments, 0)

    savings, shortfall = savings_and_shortfall(
        expected_income, sum(spending.values()) + debt
    )
    return Suggestion(
        spending=spending, debt=debt, savings=savings, shortfall=shortfall
    )


# --- Persistence --------------------------------------------------------------


@dataclass(frozen=True)
class LineView:
    category_id: uuid.UUID
    slug: str
    name: str
    suggested: int
    allocated: int
    is_user_set: bool
    spent: int


@dataclass(frozen=True)
class BudgetView:
    month: date
    currency: str
    expected_income: int
    # Spending lines, largest allocation first.
    lines: list[LineView]
    savings: LineView | None
    debt: LineView | None
    total_allocated: int
    total_spent: int
    shortfall: int | None
    uncategorised_spent: int
    uncategorised_count: int


class NotBudgetable(Exception):
    """`income` or `transfers`: money that moves, not money spent."""


async def budget_for(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    month: date,
    today: date,
    *,
    ready: bool,
) -> BudgetView:
    """[month]'s budget, generated or regenerated as the rules above say.

    While the household is still learning ([ready] false) nothing is
    generated, and nothing is written: the view holds only the lines the user
    set by hand, if any.
    """
    if not ready:
        budget = await _existing(session, household_id, month)
        return await _view(session, budget, household_id, currency, month)
    budget = await _prepared(session, household_id, currency, month, today, ready)
    await session.flush()
    return await _view(session, budget, household_id, currency, month)


async def set_line(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    month: date,
    today: date,
    category: Category,
    amount: int,
    *,
    ready: bool,
) -> BudgetView:
    """Set [category]'s line by hand, creating it if there was none.

    Manual budgeting is always available, so a category the generator left
    out — or one with no history at all, or a household still learning — can
    still be given a line.
    """
    if category.slug in NOT_BUDGETABLE:
        raise NotBudgetable(category.slug)
    budget = await _prepared(session, household_id, currency, month, today, ready)
    system = await _system_ids(session)
    key = _line_key(category.id, category.slug, system)
    line = _find(budget, key)
    if line is None:
        line = BudgetLine(
            category_id=key,
            allocated_minor_units=amount,
            suggested_minor_units=0,
            currency=currency,
            is_user_set=True,
        )
        budget.lines.append(line)
    else:
        line.allocated_minor_units = amount
        line.is_user_set = True
    await _settle(session, budget, household_id, currency, system, ready)
    return await _view(session, budget, household_id, currency, month)


async def reset_line(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    month: date,
    today: date,
    category: Category,
    *,
    ready: bool,
) -> BudgetView | None:
    """Put [category]'s line back to its suggestion. None when it has no line.

    A hand-added line with nothing to go back to is removed rather than left
    at zero: there is no suggestion for it to return to.
    """
    budget = await _prepared(session, household_id, currency, month, today, ready)
    system = await _system_ids(session)
    line = _find(budget, _line_key(category.id, category.slug, system))
    if line is None:
        return None
    if line.is_user_set:
        line.is_user_set = False
        if line.suggested_minor_units > 0:
            line.allocated_minor_units = line.suggested_minor_units
        else:
            budget.lines.remove(line)
    await _settle(session, budget, household_id, currency, system, ready)
    return await _view(session, budget, household_id, currency, month)


async def _settle(
    session: AsyncSession,
    budget: Budget,
    household_id: uuid.UUID,
    currency: str,
    system: Mapping[str, uuid.UUID],
    ready: bool,
) -> None:
    """After a hand edit: rebalance savings, unless still learning.

    While learning the budget holds only what the user typed; a savings line
    worked out from it would be a generated line in all but name.
    """
    if ready:
        income = await _income_for(session, budget, household_id, currency)
        _rebalance_savings(budget, income, system[SAVINGS_SLUG], currency)
    budget.is_user_modified = any(line.is_user_set for line in budget.lines)
    await session.flush()


async def _existing(
    session: AsyncSession, household_id: uuid.UUID, month: date
) -> Budget | None:
    return await session.scalar(
        select(Budget)
        .where(
            Budget.household_id == household_id,
            Budget.period_start == month_bounds(month)[0],
        )
        .options(selectinload(Budget.lines))
    )


async def _prepared(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    month: date,
    today: date,
    ready: bool,
) -> Budget:
    """The month's budget row, locked, with its lines brought up to date.

    Locked so two reads of the same month at once — the app fires several
    requests on launch — cannot both generate lines and collide on
    `uq_budget_line_budget_category`. The second waits and then regenerates
    from what the first wrote.

    Regenerated while the month is running, and for a month that has ended
    until a generation has had some history to work from — so a past month
    read before its statements were imported fills in once they are. Never
    while the household is still learning: a budget generated then would be
    built from too little, and for a past month frozen that way.
    """
    first, last = month_bounds(month)
    await session.execute(
        pg_insert(Budget.__table__)
        .values(
            id=uuid.uuid4(),
            household_id=household_id,
            period_start=first,
            period_end=last,
            currency=currency,
            is_user_modified=False,
        )
        .on_conflict_do_nothing(index_elements=["household_id", "period_start"])
    )
    budget = await session.scalar(
        select(Budget)
        .where(Budget.household_id == household_id, Budget.period_start == first)
        .options(selectinload(Budget.lines))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert budget is not None  # inserted above, or there already
    if ready and (last >= today or not budget.has_history):
        await _regenerate(session, budget, household_id, currency, month)
    return budget


async def _regenerate(
    session: AsyncSession,
    budget: Budget,
    household_id: uuid.UUID,
    currency: str,
    month: date,
) -> None:
    system = await _system_ids(session)
    window, history = await _history(session, household_id, currency, month)
    income = await _expected_income(session, household_id, currency)
    minimums = await session.scalar(
        select(func.coalesce(func.sum(Debt.minimum_payment_minor_units), 0)).where(
            Debt.household_id == household_id, Debt.currency == currency
        )
    )
    suggestion = suggest(history, income, int(minimums), currency)
    budget.has_history = bool(window)
    budget.expected_income_minor_units = income

    targets = dict(suggestion.spending)
    if suggestion.debt > 0:
        targets[system[DEBT_PAYMENT_SLUG]] = suggestion.debt
    savings_id = system[SAVINGS_SLUG]

    existing = {line.category_id: line for line in budget.lines}
    for category_id, line in existing.items():
        if category_id == savings_id:
            continue
        amount = targets.get(category_id, 0)
        line.suggested_minor_units = amount
        if line.is_user_set:
            continue
        if amount > 0:
            line.allocated_minor_units = amount
        else:
            budget.lines.remove(line)
    for category_id, amount in targets.items():
        if category_id not in existing:
            budget.lines.append(
                BudgetLine(
                    category_id=category_id,
                    allocated_minor_units=amount,
                    suggested_minor_units=amount,
                    currency=currency,
                    is_user_set=False,
                )
            )
    _rebalance_savings(budget, income, savings_id, currency)


def _rebalance_savings(
    budget: Budget, income: int, savings_id: uuid.UUID, currency: str
) -> None:
    """Savings takes what the other lines leave, after the user's choices.

    Run after every change, so raising a line by hand lowers savings (or opens
    a shortfall) on the same response rather than on the next read.
    """
    others = sum(
        line.allocated_minor_units
        for line in budget.lines
        if line.category_id != savings_id
    )
    remainder, _ = savings_and_shortfall(income, others)
    line = _find(budget, savings_id)
    if line is None:
        if remainder > 0:
            budget.lines.append(
                BudgetLine(
                    category_id=savings_id,
                    allocated_minor_units=remainder,
                    suggested_minor_units=remainder,
                    currency=currency,
                    is_user_set=False,
                )
            )
    else:
        line.suggested_minor_units = remainder
        if not line.is_user_set:
            if remainder > 0:
                line.allocated_minor_units = remainder
            else:
                budget.lines.remove(line)
    budget.is_user_modified = any(line.is_user_set for line in budget.lines)


async def _history(
    session: AsyncSession, household_id: uuid.UUID, currency: str, month: date
) -> tuple[list[date], dict[CategoryRef, list[int]]]:
    """The window before [month], and each category's monthly debit totals
    over it.

    The window is the three months before [month], less any before the
    household's first countable row: months we hold no statement for are not
    months of zero spending.
    """
    earliest = await session.scalar(
        select(func.min(Transaction.occurred_on)).where(
            *countable(household_id, currency)
        )
    )
    window = [
        m
        for m in months_back(month, WINDOW_MONTHS + 1)[:-1]
        if earliest is not None and m >= earliest.replace(day=1)
    ]
    if not window:
        return window, {}
    position = {m: i for i, m in enumerate(window)}
    bucket = func.date_trunc("month", Transaction.occurred_on)
    result = await session.execute(
        select(
            Transaction.category_id,
            Category.slug,
            bucket,
            func.sum(Transaction.amount_minor_units),
        )
        .join(Category, Category.id == Transaction.category_id)
        .where(
            *countable(household_id, currency),
            Transaction.direction == TransactionDirection.debit,
            Transaction.occurred_on >= window[0],
            Transaction.occurred_on <= month_bounds(window[-1])[1],
        )
        .group_by(Transaction.category_id, Category.slug, bucket)
    )
    history: dict[CategoryRef, list[int]] = defaultdict(lambda: [0] * len(window))
    for category_id, slug, at, total in result:
        history[CategoryRef(category_id, slug)][position[at.date()]] = int(total)
    return window, dict(history)


async def _expected_income(
    session: AsyncSession, household_id: uuid.UUID, currency: str
) -> int:
    """The wizard's monthly income, or zero.

    Zero when unanswered or recorded in another currency: income in CAD
    cannot be budgeted against spending in USD without a rate we do not have.
    """
    profile = await session.scalar(
        select(FinancialProfile).where(FinancialProfile.household_id == household_id)
    )
    if (
        profile is None
        or profile.monthly_income_minor_units is None
        or profile.currency != currency
    ):
        return 0
    return profile.monthly_income_minor_units


async def _income_for(
    session: AsyncSession, budget: Budget | None, household_id: uuid.UUID, currency: str
) -> int:
    """The income a budget is measured against.

    The one it was generated against, once it has been generated from
    history — so a month that has ended keeps its savings and shortfall when
    the wizard changes later. Today's figure before that (still learning, or
    no history yet), which is also what a running month stores on every read.
    """
    if budget is not None and budget.has_history:
        return budget.expected_income_minor_units
    return await _expected_income(session, household_id, currency)


async def _system_ids(session: AsyncSession) -> dict[str, uuid.UUID]:
    result = await session.execute(
        select(Category.slug, Category.id).where(
            Category.household_id.is_(None), Category.slug.in_(DEDICATED)
        )
    )
    return dict(result.all())


def _line_key(
    category_id: uuid.UUID, slug: str, system: Mapping[str, uuid.UUID]
) -> uuid.UUID:
    """The line a category's money belongs to.

    Its own, except for the dedicated slugs: a household's own `savings` or
    `debt_payment` category feeds the one system line, so there is never a
    second savings line competing for the remainder.
    """
    return system[slug] if slug in DEDICATED else category_id


def _find(budget: Budget, category_id: uuid.UUID) -> BudgetLine | None:
    return next(
        (line for line in budget.lines if line.category_id == category_id), None
    )


async def _view(
    session: AsyncSession,
    budget: Budget | None,
    household_id: uuid.UUID,
    currency: str,
    month: date,
) -> BudgetView:
    system = await _system_ids(session)
    first, last = month_bounds(month)
    result = await session.execute(
        select(
            Transaction.category_id,
            Category.slug,
            func.sum(Transaction.amount_minor_units),
            func.count(Transaction.id),
        )
        .outerjoin(Category, Category.id == Transaction.category_id)
        .where(
            *countable(household_id, currency),
            Transaction.direction == TransactionDirection.debit,
            Transaction.occurred_on >= first,
            Transaction.occurred_on <= last,
        )
        .group_by(Transaction.category_id, Category.slug)
    )
    spent: dict[uuid.UUID, int] = defaultdict(int)
    uncategorised_spent = uncategorised_count = 0
    for category_id, slug, total, count in result:
        if category_id is None:
            uncategorised_spent += int(total)
            uncategorised_count += int(count)
        else:
            spent[_line_key(category_id, slug, system)] += int(total)

    own = budget.lines if budget is not None else []
    ids = [line.category_id for line in own]
    categories = {
        category.id: category
        for category in (
            await session.scalars(select(Category).where(Category.id.in_(ids)))
        )
    }

    def view(line: BudgetLine) -> LineView:
        category = categories[line.category_id]
        return LineView(
            category_id=line.category_id,
            slug=category.slug,
            name=category.name,
            suggested=line.suggested_minor_units,
            allocated=line.allocated_minor_units,
            is_user_set=line.is_user_set,
            spent=spent.get(line.category_id, 0),
        )

    views = [view(line) for line in own]
    savings = next((v for v in views if v.category_id == system[SAVINGS_SLUG]), None)
    debt = next((v for v in views if v.category_id == system[DEBT_PAYMENT_SLUG]), None)
    lines = sorted(
        (v for v in views if v.category_id not in {system[s] for s in DEDICATED}),
        key=lambda v: (-v.allocated, v.name),
    )
    income = await _income_for(session, budget, household_id, currency)
    total_allocated = sum(v.allocated for v in views)
    over = total_allocated - income
    return BudgetView(
        month=first,
        currency=currency,
        expected_income=income,
        lines=lines,
        savings=savings,
        debt=debt,
        total_allocated=total_allocated,
        total_spent=sum(v.spent for v in views),
        shortfall=over if over > 0 else None,
        uncategorised_spent=uncategorised_spent,
        uncategorised_count=uncategorised_count,
    )
