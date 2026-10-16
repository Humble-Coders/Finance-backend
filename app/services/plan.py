"""The plan: where the money left over goes, debt payoff first (#76, PRD F11/F4).

Pure arithmetic — no database, no model — so every rule is table-tested and the
same inputs always give the same plan. Amounts are integer minor units; rates
are basis points (20.99% is 2099); `Decimal` wherever a fraction appears.
`plan_view` (the I/O half) gathers the inputs and calls these.

**The split** of a month's money left over (the budget's Savings line), in
order:

1. **The buffer**, until its emergency-fund goal reaches the target the person
   chose. No buffer if they chose none.
2. **Goals.** While any debt is above the person's rate line, only their goal
   share of what remains; otherwise all of it. Goals with a date ahead take
   what they need to stay on pace (`goals.monthly_need`), by priority; goals
   without one share what is left, by priority, up to what they still need.
3. **Extra debt payment** — the rest, while any debt is above the line.
4. **Save or invest** — what is left when none is. One generic bucket: the
   app never names a product or a security (PRD §1).

The four add up to the money left over exactly, to the minor unit.

**The schedule** runs month by month from the month asked about:

* Each debt accrues interest at its rate for that month — the promotional rate
  until the month it ends, the normal rate after — of balance × rate ÷ 12,
  rounded half up to the minor unit, every month.
* A debt above the line is **attacked**: its minimum, plus the extra, plus the
  minimums of attacked debts already paid off — the payment *rolls over* — go
  to the attacked debts, minimums first, then the rest to the first in the
  strategy's order (avalanche: highest rate that month, then smallest
  balance; snowball: smallest balance, then highest rate), and on down.
* A debt at or below the line pays only its own minimum. A promotion ending
  can lift it above the line, and it joins the attack from that month.
* A debt with no rate is left out — never given a guessed one — and said so.
* The run stops when every debt is paid, at `MAX_MONTHS`, or when a year
  passes with the total owed no lower: a payment that never covers the
  interest is reported as never paid off, not looped on.

**The comparisons:** the same debts paid by their minimums alone, each on its
own, gives the interest saved; the other strategy gives what it would cost
more (or less). Either is None when a debt is never paid in that run — there
is nothing finite to compare.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.core.money import round_up_to_whole_unit

__all__ = [
    "AVALANCHE",
    "DEFAULT_GOAL_SHARE_BPS",
    "DEFAULT_RATE_LINE_BPS",
    "MAX_MONTHS",
    "SNOWBALL",
    "DebtOutcome",
    "DebtTerms",
    "GoalAsk",
    "Preferences",
    "Schedule",
    "Split",
    "rate_on",
    "schedule",
    "split",
    "suggest_buffer",
]

AVALANCHE = "avalanche"
SNOWBALL = "snowball"

# The starting values the Your plan screen suggests (manager, 2026-10-17).
# Suggestions only: the person changes any of them.
DEFAULT_GOAL_SHARE_BPS = 2000  # 20% of what remains, to goals
DEFAULT_RATE_LINE_BPS = 600  # 6.00%: above it, paying the debt beats saving

# Fifty years. A plan longer than this is not a plan.
MAX_MONTHS = 600
# A year with the total owed no lower: the payments do not cover the interest.
STALL_MONTHS = 12

_BPS = Decimal(10_000)
_MONTHS_A_YEAR = 12


@dataclass(frozen=True)
class DebtTerms:
    id: uuid.UUID
    name: str
    balance: int
    # What must be paid each month; 0 when the person did not say.
    minimum: int
    rate_bps: int | None
    promo_rate_bps: int | None = None
    # The promotional rate applies to months starting before this day.
    promo_ends_on: date | None = None


@dataclass(frozen=True)
class GoalAsk:
    id: uuid.UUID
    # What it needs this month to stay on pace; 0 without a date ahead.
    need: int
    # What is still left to save.
    remaining: int


@dataclass(frozen=True)
class Preferences:
    # The buffer's target, or None for no buffer; and what is saved toward it.
    buffer_target: int | None
    buffer_saved: int
    goal_share_bps: int
    rate_line_bps: int
    strategy: str


@dataclass(frozen=True)
class Split:
    buffer: int
    goals: tuple[tuple[uuid.UUID, int], ...]
    extra_debt: int
    save_or_invest: int

    @property
    def total(self) -> int:
        return (
            self.buffer
            + sum(a for _, a in self.goals)
            + self.extra_debt
            + self.save_or_invest
        )


@dataclass(frozen=True)
class DebtOutcome:
    id: uuid.UUID
    # Position in the payoff order among the debts that get extra money, from 1;
    # None for one that only ever gets its minimum, or has no rate.
    order: int | None
    # Never above the line: it only gets its minimum.
    minimum_only: bool
    # The first day of the month it is paid off; None if never in this run.
    paid_off: date | None
    interest: int
    # Still owed when the run ended: the payment does not cover the interest.
    never_paid_off: bool
    # No rate to work with; left out of the schedule.
    missing_rate: bool


@dataclass(frozen=True)
class Schedule:
    debts: tuple[DebtOutcome, ...]
    # None when a debt is never paid off: the total would be unbounded.
    total_interest: int | None
    # The month the last debt is paid off; None if one never is.
    debt_free: date | None


def _add_months(month: date, count: int) -> date:
    index = month.year * 12 + (month.month - 1) + count
    return date(index // 12, index % 12 + 1, 1)


def rate_on(debt: DebtTerms, month: date) -> int | None:
    """The rate charged in [month]: the promotional one until it ends."""
    if (
        debt.promo_rate_bps is not None
        and debt.promo_ends_on is not None
        and month < debt.promo_ends_on
    ):
        return debt.promo_rate_bps
    return debt.rate_bps


def _above(debt: DebtTerms, month: date, line_bps: int) -> bool:
    rate = rate_on(debt, month)
    return rate is not None and rate > line_bps


def _interest(balance: int, rate_bps: int) -> int:
    """A month's interest on [balance], rounded half up to the minor unit."""
    if balance <= 0 or rate_bps <= 0:
        return 0
    return int(
        (Decimal(balance) * rate_bps / _BPS / _MONTHS_A_YEAR).quantize(
            Decimal(1), rounding=ROUND_HALF_UP
        )
    )


def suggest_buffer(
    monthly_expenses: list[int], declared_expense: int | None, currency: str
) -> int | None:
    """One month of the household's own spending, as the starting buffer.

    The average of the months with statements, else what setup was told, else
    nothing to suggest. Rounded **up** to a whole currency unit by
    `money.round_up_to_whole_unit`, the one place amounts change form.
    """
    if monthly_expenses:
        average = Decimal(sum(monthly_expenses)) / len(monthly_expenses)
    elif declared_expense:
        average = Decimal(declared_expense)
    else:
        return None
    return round_up_to_whole_unit(average, currency)


def split(
    leftover: int,
    preferences: Preferences,
    goals: list[GoalAsk],
    debts: list[DebtTerms],
    month: date,
) -> Split:
    """This month's money left over, divided as the module says. [goals] are in
    priority order, without the buffer's own goal."""
    left = max(0, leftover)

    buffer = 0
    if preferences.buffer_target is not None:
        buffer = min(left, max(0, preferences.buffer_target - preferences.buffer_saved))
    left -= buffer

    expensive = any(
        debt.balance > 0 and _above(debt, month, preferences.rate_line_bps)
        for debt in debts
    )
    pool = left * preferences.goal_share_bps // 10_000 if expensive else left

    given: dict[uuid.UUID, int] = {}
    for goal in goals:  # those with a date ahead, up to what keeps them on pace
        if goal.need > 0 and pool > 0:
            amount = min(pool, goal.need, goal.remaining)
            if amount > 0:
                given[goal.id] = amount
                pool -= amount
    for goal in goals:  # then those without one, up to what they still need
        if goal.need == 0 and pool > 0:
            amount = min(pool, goal.remaining)
            if amount > 0:
                given[goal.id] = amount
                pool -= amount

    to_goals = sum(given.values())
    left -= to_goals
    return Split(
        buffer=buffer,
        goals=tuple((goal.id, given[goal.id]) for goal in goals if goal.id in given),
        extra_debt=left if expensive else 0,
        save_or_invest=0 if expensive else left,
    )


@dataclass
class _Running:
    terms: DebtTerms
    balance: int
    interest: int = 0
    paid_off: date | None = None
    attacked: bool = False
    first_attacked: int | None = None
    order_key: tuple = field(default_factory=tuple)


def schedule(
    debts: list[DebtTerms],
    extra: int,
    start: date,
    line_bps: int,
    strategy: str,
    *,
    rollover: bool = True,
) -> Schedule:
    """The month-by-month payoff of [debts] from [start]'s month.

    With `rollover` false, each debt pays only its own minimum and nothing
    else — the minimums-only comparison.
    """
    start = start.replace(day=1)
    known = [debt for debt in debts if debt.rate_bps is not None]
    missing = [debt for debt in debts if debt.rate_bps is None]
    running = {debt.id: _Running(debt, max(0, debt.balance)) for debt in known}
    for item in running.values():
        if item.balance == 0:
            item.paid_off = start

    totals: list[int] = []
    month = start
    for index in range(MAX_MONTHS):
        open_debts = [r for r in running.values() if r.balance > 0]
        if not open_debts:
            break
        for item in open_debts:
            rate = rate_on(item.terms, month) or 0
            charged = _interest(item.balance, rate)
            item.balance += charged
            item.interest += charged
            if rollover and not item.attacked and _above(item.terms, month, line_bps):
                item.attacked = True
                item.first_attacked = index

        # Each debt outside the attack pays its own minimum.
        for item in open_debts:
            if not item.attacked:
                item.balance -= min(item.balance, item.terms.minimum)

        if rollover:
            # Every attacked debt's minimum stays in the attack once it is
            # paid off — the payment rolls on to the next.
            money = extra + sum(r.terms.minimum for r in running.values() if r.attacked)
            attack = [r for r in open_debts if r.attacked]
            for item in attack:
                pay = min(item.balance, item.terms.minimum, money)
                item.balance -= pay
                money -= pay
            if strategy == SNOWBALL:
                attack.sort(key=lambda r: (r.balance, -(rate_on(r.terms, month) or 0)))
            else:
                attack.sort(key=lambda r: (-(rate_on(r.terms, month) or 0), r.balance))
            for item in attack:
                if money <= 0:
                    break
                pay = min(item.balance, money)
                item.balance -= pay
                money -= pay

        for item in open_debts:
            if item.balance == 0 and item.paid_off is None:
                item.paid_off = month

        owed = sum(r.balance for r in running.values())
        totals.append(owed)
        if len(totals) > STALL_MONTHS and owed >= totals[-1 - STALL_MONTHS]:
            break
        month = _add_months(month, 1)

    attacked = sorted(
        (r for r in running.values() if r.attacked),
        key=lambda r: (r.paid_off or date.max, r.first_attacked or 0, r.terms.name),
    )
    order = {r.terms.id: position for position, r in enumerate(attacked, start=1)}

    outcomes: list[DebtOutcome] = []
    for debt in debts:
        item = running.get(debt.id)
        if item is None:
            outcomes.append(
                DebtOutcome(debt.id, None, False, None, 0, False, missing_rate=True)
            )
            continue
        never = item.balance > 0
        outcomes.append(
            DebtOutcome(
                id=debt.id,
                order=order.get(debt.id),
                minimum_only=not item.attacked,
                paid_off=item.paid_off,
                interest=item.interest,
                never_paid_off=never,
                missing_rate=False,
            )
        )

    unpaid = any(o.never_paid_off for o in outcomes)
    paid_months = [o.paid_off for o in outcomes if o.paid_off is not None]
    return Schedule(
        debts=tuple(outcomes),
        total_interest=None if unpaid else sum(o.interest for o in outcomes),
        debt_free=None if unpaid or missing or not paid_months else max(paid_months),
    )
