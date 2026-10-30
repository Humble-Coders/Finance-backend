"""The plan: where the money left over goes, debt payoff first (#76, PRD F11/F4).

Pure arithmetic — no database, no model — so every rule is table-tested and the
same inputs always give the same plan. Amounts are integer minor units; rates
are basis points (20.99% is 2099); `Decimal` wherever a fraction appears.
`plan_view` (the I/O half) gathers the inputs and calls these.

**The split** of a month's money left over (the budget's Savings line), by the
person's choices (redesign, 2026-10-31): the four categories — their
Emergency fund goal (only when they have one), extra debt payment, their
other goals, and save or invest — in the order they put them, each with a
percentage, the percentages adding up to 100.

1. **A buffer** comes off the top only when one is passed in: a diagnostic's
   own savings figure (`plan_diagnostics`). Your plan itself has none.
2. Each category takes its percentage of what is left, never more than it can
   use: the Emergency fund and each goal what they still need, the debts
   what is still owed. Save or invest has no limit.
3. What a category could not use goes on down the priorities, each topped up
   to what it can use; anything still over goes to save or invest.
4. Inside **goals**, goals with a date ahead take what keeps them on pace
   (`goals.monthly_need`), by priority; then the rest up to what each still
   needs, by priority.

The AI may propose the amounts instead (`plan_split`); `checked` holds a
proposal to the same limits. Either way the parts add up to the money left
over exactly, to the minor unit. One generic "save or invest" bucket: the app
never names a product or a security (PRD §1).

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
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from app.core.money import round_up_to_whole_unit

__all__ = [
    "AVALANCHE",
    "CATEGORIES",
    "DEBT",
    "DEFAULT_GOAL_SHARE_BPS",
    "DEFAULT_RATE_LINE_BPS",
    "EMERGENCY",
    "GOALS",
    "MAX_MONTHS",
    "SAVE_OR_INVEST",
    "SNOWBALL",
    "DebtOutcome",
    "DebtTerms",
    "GoalAsk",
    "Limits",
    "Preferences",
    "Schedule",
    "Split",
    "Weight",
    "checked",
    "debt_first",
    "edited",
    "rate_on",
    "schedule",
    "split",
    "suggest_buffer",
    "suggest_weights",
]

AVALANCHE = "avalanche"
SNOWBALL = "snowball"

# The starting values the Your plan screen suggests (manager, 2026-10-17).
# Suggestions only: the person changes any of them.
DEFAULT_GOAL_SHARE_BPS = 2000  # 20% of what remains, to goals
DEFAULT_RATE_LINE_BPS = 600  # 6.00%: above it, paying the debt beats saving

# The four places the money left over can go, as the person orders them.
EMERGENCY = "emergency_fund"
DEBT = "debt"
GOALS = "goals"
SAVE_OR_INVEST = "save_or_invest"
CATEGORIES = (EMERGENCY, DEBT, GOALS, SAVE_OR_INVEST)

# Where a suggestion starts, before it is fitted to what the household has:
# the Emergency fund and debt first, as the plan always put them.
_SUGGESTED_BPS = (
    (EMERGENCY, 2000),
    (DEBT, 5000),
    (GOALS, 2000),
    (SAVE_OR_INVEST, 1000),
)

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
class Weight:
    category: str
    bps: int


@dataclass(frozen=True)
class Preferences:
    # In priority order, adding up to 10,000.
    weights: tuple[Weight, ...]
    rate_line_bps: int
    strategy: str
    # What comes off the top first: a diagnostic's savings figure; else 0.
    buffer: int = 0


@dataclass(frozen=True)
class Limits:
    """The most each category can use this month."""

    # The Emergency fund goal's id and what it still needs; None without one.
    emergency: tuple[uuid.UUID, int] | None
    # The other goals, in priority order (without the Emergency fund).
    goals: tuple[GoalAsk, ...]
    # Everything still owed on the debts.
    debt: int

    def of(self, category: str) -> int | None:
        if category == EMERGENCY:
            return self.emergency[1] if self.emergency else 0
        if category == GOALS:
            return sum(g.remaining for g in self.goals)
        if category == DEBT:
            return self.debt
        return None  # save or invest: no limit


@dataclass(frozen=True)
class Split:
    buffer: int
    goals: tuple[tuple[uuid.UUID, int], ...]
    extra_debt: int
    save_or_invest: int
    # To the Emergency fund goal.
    emergency: int = 0

    @property
    def total(self) -> int:
        return (
            self.buffer
            + self.emergency
            + sum(a for _, a in self.goals)
            + self.extra_debt
            + self.save_or_invest
        )

    def amount(self, category: str) -> int:
        return {
            EMERGENCY: self.emergency,
            DEBT: self.extra_debt,
            GOALS: sum(a for _, a in self.goals),
            SAVE_OR_INVEST: self.save_or_invest,
        }[category]


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


def suggest_weights(limits: Limits) -> tuple[Weight, ...]:
    """The starting choices, fitted to what the household has: a category with
    nothing to give to (no Emergency fund goal, no debt, no other goal) is
    left out, and its share spread over the rest in proportion, so the
    percentages still add up to 100."""
    present = [
        (c, bps)
        for c, bps in _SUGGESTED_BPS
        if c == SAVE_OR_INVEST or (limits.of(c) or 0) > 0
    ]
    total = sum(bps for _, bps in present)
    weights = [Weight(c, bps * 10_000 // total) for c, bps in present]
    short = 10_000 - sum(w.bps for w in weights)
    # The rounding's few basis points go to the first, so the sum is exact.
    weights[0] = Weight(weights[0].category, weights[0].bps + short)
    return tuple(weights)


def debt_first(weights: tuple[Weight, ...]) -> tuple[Weight, ...]:
    """The other order a diagnostic weighs: debt at the top with the goals'
    share added to it, the goals given nothing; the rest as the person has it."""
    goals = next((w.bps for w in weights if w.category == GOALS), 0)
    debt = next((w.bps for w in weights if w.category == DEBT), 0)
    rest = [w for w in weights if w.category not in (DEBT, GOALS)]
    return (Weight(DEBT, debt + goals), *rest, Weight(GOALS, 0))


def _goal_amounts(
    amount: int, goals: tuple[GoalAsk, ...]
) -> tuple[tuple[uuid.UUID, int], ...]:
    """[amount] across the goals: dated ones to their pace, by priority; then
    each up to what it still needs, by priority."""
    given: dict[uuid.UUID, int] = {}
    pool = amount
    for goal in goals:
        if goal.need > 0 and pool > 0:
            share = min(pool, goal.need, goal.remaining)
            if share > 0:
                given[goal.id] = share
                pool -= share
    for goal in goals:
        room = goal.remaining - given.get(goal.id, 0)
        if pool > 0 and room > 0:
            share = min(pool, room)
            given[goal.id] = given.get(goal.id, 0) + share
            pool -= share
    return tuple((g.id, given[g.id]) for g in goals if given.get(g.id))


def _fill(left: int, order: list[str], amounts: dict[str, int], limits: Limits) -> int:
    """Top each category in [order] up to what it can use; what is left."""
    for category in order:
        cap = limits.of(category)
        room = left if cap is None else min(left, max(cap - amounts[category], 0))
        amounts[category] += room
        left -= room
    return left


def _assemble(buffer: int, amounts: dict[str, int], limits: Limits) -> Split:
    return Split(
        buffer=buffer,
        emergency=amounts[EMERGENCY],
        goals=_goal_amounts(amounts[GOALS], limits.goals),
        extra_debt=amounts[DEBT],
        save_or_invest=amounts[SAVE_OR_INVEST],
    )


def split(leftover: int, preferences: Preferences, limits: Limits) -> Split:
    """This month's money left over, divided as the module says."""
    left = max(0, leftover)
    buffer = min(left, max(preferences.buffer, 0))
    left -= buffer
    pool = left
    amounts = dict.fromkeys(CATEGORIES, 0)
    order = [w.category for w in preferences.weights]
    for weight in preferences.weights:
        cap = limits.of(weight.category)
        share = pool * weight.bps // 10_000
        give = min(share, left) if cap is None else min(share, cap, left)
        amounts[weight.category] += give
        left -= give
    left = _fill(left, order, amounts, limits)
    amounts[SAVE_OR_INVEST] += left
    return _assemble(buffer, amounts, limits)


def checked(
    leftover: int,
    preferences: Preferences,
    limits: Limits,
    proposed: dict[str, int],
    proposed_goals: dict[uuid.UUID, int],
) -> Split:
    """An AI's proposal held to the same rules as `split`: none negative, none
    over what its category can use, the whole exactly the money left over.

    Too much is taken back from the lowest priority up; too little is topped
    up by priority, the rest to save or invest. The goals' amounts are the
    proposal's where it named them, each within what the goal still needs."""
    left = max(0, leftover)
    order = [w.category for w in preferences.weights]
    amounts = dict.fromkeys(CATEGORIES, 0)
    goal_room = {g.id: g.remaining for g in limits.goals}
    goals_given = {
        gid: min(max(int(v), 0), goal_room[gid])
        for gid, v in proposed_goals.items()
        if gid in goal_room
    }
    for category in CATEGORIES:
        value = max(int(proposed.get(category, 0)), 0)
        if category == GOALS and goals_given:
            value = sum(goals_given.values())
        cap = limits.of(category)
        amounts[category] = value if cap is None else min(value, cap)
    over = sum(amounts.values()) - left
    for category in reversed(order + [c for c in CATEGORIES if c not in order]):
        if over <= 0:
            break
        taken = min(over, amounts[category])
        amounts[category] -= taken
        over -= taken
    remaining = left - sum(amounts.values())
    remaining = _fill(remaining, order, amounts, limits)
    amounts[SAVE_OR_INVEST] += remaining
    result = _assemble(0, amounts, limits)
    if goals_given and amounts[GOALS] == sum(goals_given.values()):
        result = replace(
            result,
            goals=tuple(
                (g.id, goals_given[g.id]) for g in limits.goals if goals_given.get(g.id)
            ),
        )
    return result


def edited(
    current: Split,
    preferences: Preferences,
    limits: Limits,
    row: str,
    amount: int,
    goal_id: uuid.UUID | None = None,
) -> Split:
    """[current] with one row set by hand, the others rebalanced by priority:
    more for it is taken from the lowest priorities first; less for it goes to
    the highest priorities that can use it, the rest to save or invest. The
    whole stays the same. Raises `ValueError` for more than the row can hold."""
    rows: dict[str, int] = {
        EMERGENCY: current.emergency,
        DEBT: current.extra_debt,
        SAVE_OR_INVEST: current.save_or_invest,
    }
    caps: dict[str, int | None] = {
        EMERGENCY: limits.of(EMERGENCY),
        DEBT: limits.debt,
        SAVE_OR_INVEST: None,
    }
    goal_rows = dict(current.goals)
    for goal in limits.goals:
        key = f"goal:{goal.id}"
        rows[key] = goal_rows.get(goal.id, 0)
        caps[key] = goal.remaining
    order: list[str] = []
    for weight in preferences.weights:
        if weight.category == GOALS:
            order += [f"goal:{g.id}" for g in limits.goals]
        else:
            order.append(weight.category)
    order += [k for k in rows if k not in order]

    key = f"goal:{goal_id}" if row == GOALS else row
    if key not in rows:
        raise ValueError("no such row")
    pool = current.total - current.buffer
    cap = caps[key]
    if amount < 0 or amount > pool or (cap is not None and amount > cap):
        raise ValueError("more than that row can hold")

    delta = amount - rows[key]
    rows[key] = amount
    others = [k for k in order if k != key]
    if delta > 0:
        for other in reversed(others):
            taken = min(delta, rows[other])
            rows[other] -= taken
            delta -= taken
            if delta == 0:
                break
    elif delta < 0:
        spare = -delta
        for other in others:
            room = (
                spare
                if caps[other] is None
                else min(spare, max(caps[other] - rows[other], 0))
            )
            rows[other] += room
            spare -= room
        rows[SAVE_OR_INVEST] += spare
    return Split(
        buffer=current.buffer,
        emergency=rows[EMERGENCY],
        goals=tuple(
            (g.id, rows[f"goal:{g.id}"]) for g in limits.goals if rows[f"goal:{g.id}"]
        ),
        extra_debt=rows[DEBT],
        save_or_invest=rows[SAVE_OR_INVEST],
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
