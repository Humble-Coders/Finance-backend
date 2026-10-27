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
* **Commitments hold their line up** the same way: a category's line is the
  larger of its median and the setup commitments in it (rounded up), so Rent
  $1,800 is budgeted from the first month, before any rent was seen.
* **Savings is a target the person sets**, never a figure filled in for
  them: until one is set there is no savings line, and the budget reports
  how much room there is for one (`savings_room`: expected income less every
  other line). Lines and target together above expected income are the
  **shortfall**.
* **A line set by hand carries forward.** A month's budget, when first
  created, starts with the hand-set lines of the latest month before it —
  a category added to the budget, a changed amount, the savings target — so
  planning something once plans it for every month after. A line changed
  **for this month only** does not: the months after start from what it
  carried before, or from the suggestion when it carried nothing.
* **A one-off purchase** (`Transaction.is_one_off`) counts in its own month
  like any spending, but is left out of the history lines are suggested
  from, so a laptop bought once never raises the months after — not even in a
  household's first months, when the window is too short for the median to
  leave it out.

"Spent" is counted by `countable`, the dashboard's rule, so a line can never
disagree with the dashboard figure beside it.

**Regeneration.** There is no worker, so a budget is (re)generated when it is
read. A month still running is recomputed on every read: lines the user set
keep their amount and only their suggestion moves. A month that has ended
keeps the budget it had once that budget was built from categorised spending;
one read before its statements were imported, or while they await review,
fills in when they are.

**Still learning.** Nothing is generated from history until the household
passes the threshold in `app/services/learning.py`. What setup already says —
the commitments and the debts' minimums, and savings as what income leaves
after them — is shown as lines from day one, without being stored; lines set
by hand are kept and shown over them: manual budgeting is always available.

**The month so far.** Beside the lines, a budget reports the month as the
statements tell it (income, spending, what is left), the commitments seen paid
or not, and — while the month is running — what is still to come: the
commitments not seen yet, the rest of each line beyond them, and the income
setup expects that has not arrived. Nothing is counted twice: a commitment's
amount is taken out of its own line's remainder.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from sqlalchemy import case, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.money import round_up_to_whole_unit
from app.models.categorization import Category
from app.models.derived import AiBudgetSuggestion
from app.models.enums import AccountKind, TransactionDirection
from app.models.money import Account, Transaction
from app.models.planning import Budget, BudgetLine, Debt
from app.models.setup import FinancialProfile
from app.services import commitments
from app.services.commitments import Commitment
from app.services.dashboard import (
    DEBT_PAYMENT_SLUG,
    SAVINGS_SLUG,
    CategorySpend,
    countable,
    figures_by_month,
    month_bounds,
    months_back,
    spend_by_category,
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
    floors: Mapping[uuid.UUID, int] | None = None,
) -> Suggestion:
    """The generated budget, from each category's monthly debit totals.

    Each sequence holds one total per month in the window, zeros included —
    the zeros are what filter one-offs. Every sequence is the same window.

    [floors] is the least a category's line holds — its setup commitments —
    whether or not it has any history.
    """
    least = {
        key: round_up_to_whole_unit(amount, currency)
        for key, amount in (floors or {}).items()
        if amount > 0
    }
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
        amount = max(
            round_up_to_whole_unit(median(totals), currency),
            least.pop(category.id, 0),
        )
        if amount > 0:
            spending[category.id] = amount
    spending.update(least)

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
    # The part of `spent` marked one-off.
    one_off: int = 0
    # Set by hand for this month alone: the months after start from something else.
    this_month_only: bool = False
    # What the AI suggests for the line this month, checked by the server
    # (`app/services/ai_budget.py`), and why; None without a suggestion.
    ai_suggested: int | None = None
    ai_reason: str | None = None


@dataclass(frozen=True)
class UnbudgetedView:
    """Spending in a category this month's budget has no line for."""

    category_id: uuid.UUID
    slug: str
    name: str
    spent: int
    ai_suggested: int | None = None
    ai_reason: str | None = None
    one_off: int = 0


@dataclass(frozen=True)
class AiLineView:
    """An AI suggestion for a category with no line and no spending yet."""

    category_id: uuid.UUID
    slug: str
    name: str
    amount: int
    reason: str | None


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
    # Spending in categories with no line — a first month has nothing to
    # learn from, and a one-off has a median of 0 — largest first.
    unbudgeted: list[UnbudgetedView] = field(default_factory=list)
    unbudgeted_spent: int = 0
    # Everything the month spent against the budget: the lines, the
    # spending with no line, and the spending with no category.
    spent_overall: int = 0
    # The month's actual income and spending, counted as Home counts them.
    month_income: int = 0
    month_spending: int = 0
    # Setup's commitments, each with the payment seen for it this month.
    commitments: list[Commitment] = field(default_factory=list)
    month_ended: bool = False
    # Where the month's money went, by category, largest first — the
    # dashboard's own breakdown, so it sums to `month_spending` to the cent.
    # What a month that has ended is shown as.
    spending_by_category: list[CategorySpend] = field(default_factory=list)
    # What is still to come; all None once the month has ended.
    income_expected: int | None = None
    commitments_due: int | None = None
    budget_rest: int | None = None
    # The savings target not yet moved to savings.
    savings_due: int | None = None
    projected_left: int | None = None
    # Money moved to savings this month, less any taken back out.
    month_saved: int = 0
    # Expected income less every line but savings: the room for a target.
    savings_room: int = 0
    savings_category_id: uuid.UUID | None = None
    # The AI's suggestions for the month (the review, else the setup one).
    ai_kind: str | None = None
    ai_extra: list[AiLineView] = field(default_factory=list)
    ai_savings: int | None = None
    ai_advice: str | None = None
    # True while the month runs, the household is ready, and no review has
    # been made for it yet: the app may ask for one.
    ai_review_due: bool = False


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
    generated, and nothing is written: the view holds the lines the user set
    by hand, and setup's lines for the rest.
    """
    if not ready:
        budget = await _existing(session, household_id, month)
        return await _view(session, budget, household_id, currency, month, today, ready)
    budget = await _prepared(session, household_id, currency, month, today, ready)
    await session.flush()
    return await _view(session, budget, household_id, currency, month, today, ready)


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
    this_month_only: bool = False,
) -> BudgetView:
    """Set [category]'s line by hand, creating it if there was none.

    Manual budgeting is always available, so a category the generator left
    out — or one with no history at all, or a household still learning — can
    still be given a line.

    [this_month_only]: the months after keep starting from what this line
    carried before the change — or, when it carried nothing, from the
    suggestion.
    """
    if category.slug in NOT_BUDGETABLE:
        raise NotBudgetable(category.slug)
    budget = await _prepared(session, household_id, currency, month, today, ready)
    system = await _system_ids(session)
    key = _line_key(category.id, category.slug, system)
    line = _find(budget, key)
    carried = _carry_of(line)
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
    if not this_month_only:
        line.carries_forward, line.carry_minor_units = True, None
    elif carried is None:
        line.carries_forward, line.carry_minor_units = False, None
    else:
        line.carries_forward, line.carry_minor_units = True, carried
    await _settle(session, budget, household_id, currency, system, ready)
    return await _view(session, budget, household_id, currency, month, today, ready)


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
        line.carries_forward, line.carry_minor_units = True, None
        if line.suggested_minor_units > 0:
            line.allocated_minor_units = line.suggested_minor_units
        else:
            budget.lines.remove(line)
    await _settle(session, budget, household_id, currency, system, ready)
    return await _view(session, budget, household_id, currency, month, today, ready)


async def _settle(
    session: AsyncSession,
    budget: Budget,
    household_id: uuid.UUID,
    currency: str,
    system: Mapping[str, uuid.UUID],
    ready: bool,
) -> None:
    """After a hand edit: bring the savings target's room up to date."""
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
    created = await session.execute(
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
    if created.rowcount:
        for line in await _carried(session, household_id, month):
            line.currency = currency
            budget.lines.append(line)
        if budget.lines:
            budget.is_user_modified = True
    if ready and (last >= today or not budget.has_history):
        await _regenerate(session, budget, household_id, currency, month)
    return budget


async def _ai_suggestion(
    session: AsyncSession, household_id: uuid.UUID, month: date
) -> AiBudgetSuggestion | None:
    """The month's AI suggestion: its review if there is one, else setup's."""
    rows = (
        await session.scalars(
            select(AiBudgetSuggestion).where(
                AiBudgetSuggestion.household_id == household_id,
                AiBudgetSuggestion.month == month,
            )
        )
    ).all()
    by_kind = {row.kind: row for row in rows}
    return by_kind.get("review") or by_kind.get("setup")


async def apply_lines(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    month: date,
    today: date,
    items: Sequence[tuple[Category, int]],
    savings: int | None,
    *,
    ready: bool,
) -> BudgetView:
    """Set several lines by hand at once — the budget the person started
    from the AI's suggestion. Each becomes a hand-set line, so it carries into
    the months after; a zero removes a hand-set line."""
    budget = await _prepared(session, household_id, currency, month, today, ready)
    system = await _system_ids(session)
    wanted = list(items)
    if savings is not None:
        savings_category = await session.get(Category, system[SAVINGS_SLUG])
        wanted.append((savings_category, savings))
    for category, amount in wanted:
        if category.slug in NOT_BUDGETABLE:
            raise NotBudgetable(category.slug)
        key = _line_key(category.id, category.slug, system)
        line = _find(budget, key)
        if amount <= 0:
            if line is not None and line.is_user_set:
                budget.lines.remove(line)
            continue
        if line is None:
            budget.lines.append(
                BudgetLine(
                    category_id=key,
                    allocated_minor_units=amount,
                    suggested_minor_units=0,
                    currency=currency,
                    is_user_set=True,
                )
            )
        else:
            line.allocated_minor_units = amount
            line.is_user_set = True
            line.carries_forward, line.carry_minor_units = True, None
    await _settle(session, budget, household_id, currency, system, ready)
    return await _view(session, budget, household_id, currency, month, today, ready)


def _carry_of(line: BudgetLine | None) -> int | None:
    """What [line] starts the months after with, or None for nothing."""
    # `carries_forward` is None on a line not yet flushed: its default, True.
    if line is None or not line.is_user_set or line.carries_forward is False:
        return None
    return (
        line.carry_minor_units
        if line.carry_minor_units is not None
        else line.allocated_minor_units
    )


def _is_this_month_only(line: BudgetLine) -> bool:
    if not line.is_user_set:
        return False
    carry = _carry_of(line)
    return carry is None or carry != line.allocated_minor_units


async def _carried(
    session: AsyncSession, household_id: uuid.UUID, month: date
) -> list[BudgetLine]:
    """The hand-set lines of the latest budget before [month]: what a new
    month's budget starts with, each at the amount it carries (`_carry_of`).
    New, unsaved lines — the caller adds them where they belong."""
    prior = await session.scalar(
        select(Budget)
        .where(
            Budget.household_id == household_id,
            Budget.period_start < month_bounds(month)[0],
        )
        .order_by(Budget.period_start.desc())
        .limit(1)
        .options(selectinload(Budget.lines))
    )
    if prior is None:
        return []
    return [
        BudgetLine(
            category_id=line.category_id,
            allocated_minor_units=carry,
            suggested_minor_units=0,
            currency=line.currency,
            is_user_set=True,
        )
        for line in prior.lines
        if (carry := _carry_of(line)) is not None
    ]


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
    minimums = await _minimums(session, household_id, currency, window)
    held = await commitments.floors(session, household_id, currency)
    suggestion = suggest(history, income, int(minimums), currency, held)
    # Categorised spending, not merely rows: a month read while last month's
    # statement is still awaiting review would otherwise lock with nothing.
    budget.has_history = bool(history)
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
    """Keep the savings line a target the person set, and its room current.

    Never creates one. A savings line the generator filled in before targets
    existed is removed; a target keeps its amount, and its `suggested` is the
    room for it — expected income less every other line — so the editor can
    say how much there is.
    """
    others = sum(
        line.allocated_minor_units
        for line in budget.lines
        if line.category_id != savings_id
    )
    line = _find(budget, savings_id)
    if line is not None:
        if line.is_user_set:
            line.suggested_minor_units = max(income - others, 0)
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
            # Spent once, never again: not what the months after will cost.
            Transaction.is_one_off.is_(False),
            Transaction.occurred_on >= window[0],
            Transaction.occurred_on <= month_bounds(window[-1])[1],
        )
        .group_by(Transaction.category_id, Category.slug, bucket)
    )
    history: dict[CategoryRef, list[int]] = defaultdict(lambda: [0] * len(window))
    for category_id, slug, at, total in result:
        history[CategoryRef(category_id, slug)][position[at.date()]] = int(total)
    return window, dict(history)


async def _minimums(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    window: list[date],
) -> int:
    """The debts' minimum payments the Debt line must at least hold.

    Without a card linked to its debt (#76) that card's minimum is counted —
    as before. With one linked whose statements are in the window, its
    purchases are already budgeted in their own lines, and paying them off is
    not more money spent: counting its minimum as well would budget the same
    money twice. A loan's minimum always counts; its statement lists no
    purchases.
    """
    budgeted_cards = None
    if window:
        budgeted_cards = (
            select(Transaction.account_id)
            .join(Account, Account.id == Transaction.account_id)
            .where(
                *countable(household_id, currency),
                Account.kind == AccountKind.credit_card,
                Transaction.occurred_on >= window[0],
                Transaction.occurred_on <= month_bounds(window[-1])[1],
            )
        )
    statement = select(
        func.coalesce(func.sum(Debt.minimum_payment_minor_units), 0)
    ).where(Debt.household_id == household_id, Debt.currency == currency)
    if budgeted_cards is not None:
        statement = statement.where(
            or_(Debt.account_id.is_(None), Debt.account_id.not_in(budgeted_cards))
        )
    return int(await session.scalar(statement) or 0)


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


async def _from_setup(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    month: date,
    system: Mapping[str, uuid.UUID],
) -> dict[uuid.UUID, int]:
    """Line -> amount, from setup alone: each category's commitments and the
    debts' minimums. What a household still learning is budgeted."""
    window, _ = await _history(session, household_id, currency, month)
    minimums = await _minimums(session, household_id, currency, window)
    held = await commitments.floors(session, household_id, currency)
    suggestion = suggest({}, 0, minimums, currency, held)
    lines = dict(suggestion.spending)
    if suggestion.debt > 0:
        lines[system[DEBT_PAYMENT_SLUG]] = suggestion.debt
    return lines


def _still_to_come(
    views: Sequence[LineView],
    savings_id: uuid.UUID,
    month_commitments: Sequence[Commitment],
    income: int,
    month_income: int,
    month_spending: int,
    saved: int,
    target: int,
) -> tuple[int, int, int, int, int]:
    """(income expected, commitments due, budget rest, savings due,
    projected left) for a month still running.

    * **Commitments due**: every commitment not seen paid yet.
    * **Budget rest**: what each line has left beyond those — a line's
      unspent allocation less the commitments still due in it, never below
      zero — so Rent unpaid is counted once, as a commitment, not again as the
      rent line's remainder. Savings is not spending and is left out.
    * **Income expected**: setup's monthly income less what has arrived.
    * **Savings due**: the savings target not yet moved to savings.
    * **Projected left**: income (received and expected) less spending, what
      is still to come, and savings — what is free for goals or more savings.
    """
    unseen = [c for c in month_commitments if c.match is None]
    due = sum(c.expected_minor_units for c in unseen)
    due_in: dict[uuid.UUID, int] = defaultdict(int)
    for item in unseen:
        if item.category_id is not None:
            due_in[item.category_id] += item.expected_minor_units
    rest = sum(
        max(v.allocated - v.spent - due_in.get(v.category_id, 0), 0)
        for v in views
        if v.category_id != savings_id
    )
    expected = max(income - month_income, 0)
    savings_due = max(target - saved, 0)
    projected = (
        month_income - month_spending - saved + expected - due - rest - savings_due
    )
    return expected, due, rest, savings_due, projected


async def _view(
    session: AsyncSession,
    budget: Budget | None,
    household_id: uuid.UUID,
    currency: str,
    month: date,
    today: date,
    ready: bool,
) -> BudgetView:
    """What a client is shown.

    While learning ([ready] false), only the lines the user set by hand. A
    budget generated earlier can still hold suggested lines — a household
    drops back below the threshold when it deletes transactions — and those
    are kept for when it is ready again, but not shown: a suggestion from
    history we have since lost is not one we stand behind. Setup's own lines
    are shown in their place (`_from_setup`): those need no history.
    """
    system = await _system_ids(session)
    first, last = month_bounds(month)
    result = await session.execute(
        select(
            Transaction.category_id,
            Category.slug,
            func.sum(Transaction.amount_minor_units),
            func.count(Transaction.id),
            func.sum(
                case(
                    (Transaction.is_one_off.is_(True), Transaction.amount_minor_units),
                    else_=0,
                )
            ),
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
    one_off: dict[uuid.UUID, int] = defaultdict(int)
    uncategorised_spent = uncategorised_count = 0
    for category_id, slug, total, count, once in result:
        if category_id is None:
            uncategorised_spent += int(total)
            uncategorised_count += int(count)
        else:
            key = _line_key(category_id, slug, system)
            spent[key] += int(total)
            one_off[key] += int(once or 0)

    own = [
        line
        for line in (
            budget.lines
            if budget is not None
            # No budget for the month yet (still learning, so nothing was
            # written): what it will start with when it is.
            else await _carried(session, household_id, month)
        )
        if ready or line.is_user_set
    ]
    # While learning: what setup alone budgets, for every line not set by hand.
    setup_lines: dict[uuid.UUID, int] = {}
    if not ready:
        setup_lines = await _from_setup(session, household_id, currency, month, system)
        for line in own:
            setup_lines.pop(line.category_id, None)
    ids = [line.category_id for line in own] + list(setup_lines)
    if not ready:
        ids.append(system[SAVINGS_SLUG])
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
            one_off=one_off.get(line.category_id, 0),
            this_month_only=_is_this_month_only(line),
        )

    views = [view(line) for line in own]
    for key, amount in setup_lines.items():
        if key == system[SAVINGS_SLUG]:
            continue
        category = categories[key]
        views.append(
            LineView(
                category_id=key,
                slug=category.slug,
                name=category.name,
                suggested=amount,
                allocated=amount,
                is_user_set=False,
                spent=spent.get(key, 0),
                one_off=one_off.get(key, 0),
            )
        )
    suggestion = await _ai_suggestion(session, household_id, first)
    ai = {
        uuid.UUID(row["category_id"]): (row["amount_minor_units"], row.get("reason"))
        for row in (suggestion.lines if suggestion else [])
    }
    views = [
        replace(v, ai_suggested=ai[v.category_id][0], ai_reason=ai[v.category_id][1])
        if v.category_id in ai
        else v
        for v in views
    ]
    savings = next((v for v in views if v.category_id == system[SAVINGS_SLUG]), None)
    debt = next((v for v in views if v.category_id == system[DEBT_PAYMENT_SLUG]), None)
    lines = sorted(
        (v for v in views if v.category_id not in {system[s] for s in DEDICATED}),
        key=lambda v: (-v.allocated, v.name),
    )
    income = await _income_for(session, budget, household_id, currency)
    total_allocated = sum(v.allocated for v in views)
    over = total_allocated - income
    total_spent = sum(v.spent for v in views)

    # Spending no line holds. Income and transfers can never hold one, and
    # money moved to savings is set aside, not spent.
    lined = {v.category_id for v in views}
    loose = {
        key: amount for key, amount in spent.items() if key not in lined and amount > 0
    }
    named = (
        {
            category.id: category
            for category in (
                await session.scalars(
                    select(Category).where(Category.id.in_(list(loose)))
                )
            )
        }
        if loose
        else {}
    )
    unbudgeted_ai = sorted(
        (
            UnbudgetedView(
                category_id=key,
                slug=named[key].slug,
                name=named[key].name,
                spent=amount,
                one_off=one_off.get(key, 0),
            )
            for key, amount in loose.items()
            if key in named
            and named[key].slug not in NOT_BUDGETABLE
            and named[key].slug != SAVINGS_SLUG
        ),
        key=lambda u: (-u.spent, u.name),
    )
    unbudgeted = [
        replace(u, ai_suggested=ai[u.category_id][0], ai_reason=ai[u.category_id][1])
        if u.category_id in ai
        else u
        for u in unbudgeted_ai
    ]
    unbudgeted_spent = sum(u.spent for u in unbudgeted)
    shown = {v.category_id for v in views} | {u.category_id for u in unbudgeted}
    extra_ids = [
        key for key in ai if key not in shown and key != system[DEBT_PAYMENT_SLUG]
    ]
    extra_names = (
        {
            c.id: c
            for c in await session.scalars(
                select(Category).where(Category.id.in_(extra_ids))
            )
        }
        if extra_ids
        else {}
    )
    ai_extra = [
        AiLineView(key, extra_names[key].slug, extra_names[key].name, *ai[key])
        for key in extra_ids
        if key in extra_names
    ]
    figures = (await figures_by_month(session, household_id, currency, [first])).get(
        first
    )
    month_income = figures.income if figures else 0
    month_spending = figures.expenses if figures else 0
    saved = (figures.invested - figures.withdrawn) if figures else 0
    others = sum(v.allocated for v in views if v.category_id != system[SAVINGS_SLUG])
    month_commitments = await commitments.for_month(
        session, household_id, currency, first, last
    )
    ended = last < today
    to_come = (
        None
        if ended
        else _still_to_come(
            views,
            system[SAVINGS_SLUG],
            month_commitments,
            income,
            month_income,
            month_spending,
            saved,
            savings.allocated if savings is not None else 0,
        )
    )
    return BudgetView(
        month=first,
        currency=currency,
        expected_income=income,
        lines=lines,
        savings=savings,
        debt=debt,
        total_allocated=total_allocated,
        total_spent=total_spent,
        shortfall=over if over > 0 else None,
        uncategorised_spent=uncategorised_spent,
        uncategorised_count=uncategorised_count,
        unbudgeted=unbudgeted,
        unbudgeted_spent=unbudgeted_spent,
        spent_overall=total_spent + unbudgeted_spent + uncategorised_spent,
        month_income=month_income,
        month_spending=month_spending,
        commitments=month_commitments,
        month_ended=ended,
        spending_by_category=await spend_by_category(
            session, household_id, currency, first
        ),
        income_expected=to_come[0] if to_come else None,
        commitments_due=to_come[1] if to_come else None,
        budget_rest=to_come[2] if to_come else None,
        savings_due=to_come[3] if to_come else None,
        projected_left=to_come[4] if to_come else None,
        month_saved=saved,
        savings_room=max(income - others, 0),
        savings_category_id=system[SAVINGS_SLUG],
        ai_kind=suggestion.kind if suggestion else None,
        ai_extra=sorted(ai_extra, key=lambda a: (-a.amount, a.name)),
        ai_savings=suggestion.savings_minor_units if suggestion else None,
        ai_advice=suggestion.advice if suggestion else None,
        ai_review_due=(
            not ended and ready and (suggestion is None or suggestion.kind != "review")
        ),
    )
