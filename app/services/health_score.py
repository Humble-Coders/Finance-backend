"""The Money Health Score (PRD F6): one number from 0 to 100, kept as history.

Two halves, as in `app/services/budget.py`:

* **`score`** is the arithmetic. Plain values in, a result out, no database —
  so every rule is table-tested, and the same inputs always give the same
  score. `Decimal` throughout; no float, and no model call anywhere in this
  module (the LLM explains a score in M6; it never produces one).
* **`current_score`** gathers the inputs, scores them, and keeps today's
  snapshot.

**Formula v3** (backend #73) — v2's arithmetic, unchanged, over corrected
inputs: a month's income and expenses no longer count money moved between
the household's own accounts (`dashboard.NOT_A_FLOW` — transfers, savings).
Saving no longer lowers `net`, and a card bill no longer inflates `income`, so
the savings rate means what it says. Bumped because the same arithmetic over
different inputs is a different score: a v2 → v3 change is partly the counting
moving, not the person.

**Formula v2** (backend #66) — v1's three parts (manager decision
2026-10-04, settling PRD OD4) plus goal completion, with v1's weights scaled
to 85 % and goals at 15 %:

* `savings_consistency` (34): over the last three complete months, each
  month's savings rate `net / income`, clamped to 0–20 % and scaled so 20 %
  scores 100; the average of the months that had income.
* `spending_vs_budget` (29.75): last complete month's spending lines (not
  savings or debt). Within allocation scores 100; over it,
  `max(0, 100 - 100 * overspend / allocated)`. Weighted by allocation.
* `debt_payments` (21.25): last complete month, `min(100, 100 * paid /
  required)`, `required` being the debts' minimum payments. No debts scores
  100: owing nothing is not a debt problem.
* `goal_completion` (15): how goals with a date are keeping pace; see
  `goal_completion`.

A component that cannot be scored is left out and the weights renormalised
over the rest; with none left there is no score. The score is the weighted
average rounded half-up to an integer.

**History without a scheduler.** There is no worker, so the score is computed
when it is read. Today's snapshot (UTC) is upserted on every read, so an
import later in the day is reflected; a past day's snapshot is never
rewritten. A day nobody opened the app has no snapshot. Until the last
complete month has any data — its statement not yet imported — nothing is
scored and the latest snapshot is held instead (see `current_score`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.derived import HealthScoreSnapshot
from app.models.planning import Debt, Goal
from app.services.budget import budget_for
from app.services.dashboard import figures_by_month, months_back
from app.services.learning import LearningState, learning_state

# --- The formula --------------------------------------------------------------
#
# Changing a weight, the cap, or any rule in `score` means bumping
# FORMULA_VERSION. Every snapshot stores the version it was computed with, so a
# trend never compares two formulas without saying so — and a snapshot whose
# breakdown no longer reproduces its score is how a silent change is caught.

FORMULA_VERSION = "v3"

SAVINGS_CONSISTENCY = "savings_consistency"
SPENDING_VS_BUDGET = "spending_vs_budget"
DEBT_PAYMENTS = "debt_payments"
GOAL_COMPLETION = "goal_completion"

# v2 (backend #66) adds goal completion at 15 and scales v1's three weights
# (40 / 35 / 25) to the remaining 85 % — so a household with no goal to count
# gets exactly the score v1 gave it, and the day v2 ships moves nobody who
# has no goals. Manager decision, 2026-10-06; the PO may retune.
WEIGHTS: dict[str, Decimal] = {
    SAVINGS_CONSISTENCY: Decimal("34"),
    SPENDING_VS_BUDGET: Decimal("29.75"),
    DEBT_PAYMENTS: Decimal("21.25"),
    GOAL_COMPLETION: Decimal("15"),
}

# How long an achieved goal keeps counting, at full marks, before it drops out:
# finishing one lifts the score, but not forever.
ACHIEVED_COUNTS_MONTHS = 12

# A savings rate at or above this scores full marks.
SAVINGS_RATE_CAP = Decimal("0.20")

# How many complete months savings consistency looks back over.
SAVINGS_WINDOW_MONTHS = 3

# How many snapshots the endpoint returns as history.
HISTORY_LENGTH = 12

_HUNDRED = Decimal(100)
_ZERO = Decimal(0)


@dataclass(frozen=True)
class MonthFlow:
    """One complete month's countable income and spending, in minor units."""

    month: date
    income: int
    expenses: int

    @property
    def net(self) -> int:
        return self.income - self.expenses


@dataclass(frozen=True)
class BudgetLineUse:
    """One spending line of last month's budget, as 4.1 serves it."""

    slug: str
    allocated: int
    spent: int


@dataclass(frozen=True)
class DebtPicture:
    # Debts recorded in setup, and how many of them carry a minimum payment.
    debts: int
    debts_with_minimum: int
    # The sum of those minimums.
    required: int
    # Last complete month's debt_payment debits.
    paid: int


@dataclass(frozen=True)
class GoalPace:
    """One goal as goal completion reads it (5.1's table). Months by their 1st."""

    target: int
    saved: int
    created_month: date
    # None for a goal without a date: no pace to judge, so it is not counted.
    target_month: date | None
    achieved_month: date | None


@dataclass(frozen=True)
class ScoreInputs:
    # Complete months in the window that have any countable rows.
    months: tuple[MonthFlow, ...]
    budget_lines: tuple[BudgetLineUse, ...]
    debt: DebtPicture
    # v2. Empty, and None, on a v1 snapshot's breakdown, which had neither.
    goals: tuple[GoalPace, ...] = ()
    last_complete_month: date | None = None


@dataclass(frozen=True)
class ComponentResult:
    key: str
    # Exact, for the arithmetic; `display_score` is what a client is shown.
    score: Decimal | None
    # The renormalised weight this component carried, 0 when unavailable.
    weight: Decimal
    available: bool

    @property
    def display_score(self) -> int | None:
        return None if self.score is None else _round(self.score)


@dataclass(frozen=True)
class ScoreResult:
    score: int | None
    formula_version: str
    components: tuple[ComponentResult, ...]


def _round(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def savings_consistency(months: tuple[MonthFlow, ...]) -> Decimal | None:
    """The average monthly savings score; None when no month had income.

    A month with no income has no savings rate to speak of — it is left out
    rather than scored zero, which would punish a gap in the statements.
    """
    scores = [
        _clamp(Decimal(m.net) / Decimal(m.income), _ZERO, SAVINGS_RATE_CAP)
        / SAVINGS_RATE_CAP
        * _HUNDRED
        for m in months
        if m.income > 0
    ]
    if not scores:
        return None
    return sum(scores, _ZERO) / len(scores)


def spending_vs_budget(lines: tuple[BudgetLineUse, ...]) -> Decimal | None:
    """Allocation-weighted line scores; None with nothing allocated.

    A line at $0 carries no weight, so a budget of only $0 lines has nothing
    to weigh and is unavailable rather than a division by zero.
    """
    allocated = sum(line.allocated for line in lines if line.allocated > 0)
    if allocated == 0:
        return None
    weighted = _ZERO
    for line in lines:
        if line.allocated <= 0:
            continue
        over = line.spent - line.allocated
        line_score = (
            _HUNDRED
            if over <= 0
            else max(_ZERO, _HUNDRED - _HUNDRED * over / line.allocated)
        )
        weighted += line_score * line.allocated
    return weighted / allocated


def debt_payments(debt: DebtPicture) -> Decimal | None:
    """Paid against required; 100 with no debts; None when no debt has a
    minimum payment to measure against."""
    if debt.debts == 0:
        return _HUNDRED
    if debt.debts_with_minimum == 0:
        return None
    if debt.required <= 0:
        return _HUNDRED
    return min(_HUNDRED, _HUNDRED * Decimal(debt.paid) / Decimal(debt.required))


def _month_index(month: date) -> int:
    return month.year * 12 + month.month - 1


def goal_completion(
    goals: tuple[GoalPace, ...], last_complete_month: date | None
) -> Decimal | None:
    """How goals are keeping pace, averaged over those that count; None if none do.

    * **Which count:** goals with a target date, created before the last
      complete month — so a full month has passed to judge pace against.
    * **Achieved:** 100 while the achievement month is within
      `ACHIEVED_COUNTS_MONTHS` of the last complete month, then left out. The
      same rule on which count applies, so a goal made and filled the same day
      is not a year of full marks.
    * **Otherwise:** pace on a straight line from the creation month to the
      target month, both counted. By the end of the last complete month a goal
      should have `target × elapsed / total`; it scores `saved` against that,
      capped at 100. Past its date, it is judged against the whole target.
      `saved` is today's — goals keep no history.
    * **The component** is the plain average, so one large target cannot
      drown out the rest.
    """
    if last_complete_month is None:
        return None
    last = _month_index(last_complete_month)
    scores: list[Decimal] = []
    for goal in goals:
        if goal.target_month is None:
            continue
        created = _month_index(goal.created_month)
        if created >= last:
            continue
        achieved = goal.achieved_month is not None or goal.saved >= goal.target
        if achieved:
            achieved_on = _month_index(goal.achieved_month or last_complete_month)
            if last - achieved_on < ACHIEVED_COUNTS_MONTHS:
                scores.append(_HUNDRED)
            continue
        total = _month_index(goal.target_month) - created + 1
        elapsed = last - created + 1
        expected = (
            Decimal(goal.target)
            if total <= 0 or elapsed >= total
            else Decimal(goal.target) * elapsed / total
        )
        scores.append(min(_HUNDRED, _HUNDRED * Decimal(goal.saved) / expected))
    if not scores:
        return None
    # Summed in a fixed order, so the same goals in any order give the same total.
    return sum(sorted(scores), _ZERO) / len(scores)


def score(inputs: ScoreInputs) -> ScoreResult:
    """The score for [inputs]: deterministic, and order-independent."""
    raw = {
        SAVINGS_CONSISTENCY: savings_consistency(inputs.months),
        SPENDING_VS_BUDGET: spending_vs_budget(inputs.budget_lines),
        DEBT_PAYMENTS: debt_payments(inputs.debt),
        GOAL_COMPLETION: goal_completion(inputs.goals, inputs.last_complete_month),
    }
    present = sum(
        (WEIGHTS[key] for key, value in raw.items() if value is not None), _ZERO
    )
    components = tuple(
        ComponentResult(
            key=key,
            score=value,
            weight=(_ZERO if value is None else WEIGHTS[key] * _HUNDRED / present),
            available=value is not None,
        )
        for key, value in raw.items()
    )
    if present == 0:
        return ScoreResult(None, FORMULA_VERSION, components)
    total = (
        sum(
            (c.score * WEIGHTS[c.key] for c in components if c.score is not None), _ZERO
        )
        / present
    )
    return ScoreResult(_round(total), FORMULA_VERSION, components)


# --- The stored breakdown -----------------------------------------------------
#
# A snapshot's `components` holds the inputs, exactly (minor units, ISO dates),
# beside what each component scored. Feeding the inputs back through `score`
# reproduces the stored score; that is what makes a past score explainable.


def to_json(inputs: ScoreInputs, result: ScoreResult) -> dict:
    return {
        "formula_version": result.formula_version,
        "inputs": {
            "months": [
                {
                    "month": m.month.isoformat(),
                    "income": m.income,
                    "expenses": m.expenses,
                }
                for m in sorted(inputs.months, key=lambda m: m.month)
            ],
            "budget_lines": [
                {"slug": line.slug, "allocated": line.allocated, "spent": line.spent}
                for line in sorted(inputs.budget_lines, key=lambda line: line.slug)
            ],
            "debt": {
                "debts": inputs.debt.debts,
                "debts_with_minimum": inputs.debt.debts_with_minimum,
                "required": inputs.debt.required,
                "paid": inputs.debt.paid,
            },
            "goals": [
                {
                    "target": g.target,
                    "saved": g.saved,
                    "created_month": g.created_month.isoformat(),
                    "target_month": _iso(g.target_month),
                    "achieved_month": _iso(g.achieved_month),
                }
                for g in sorted(inputs.goals, key=_goal_order)
            ],
            "last_complete_month": _iso(inputs.last_complete_month),
        },
        "components": [
            {
                "key": c.key,
                "score": None if c.score is None else str(c.score),
                "weight": str(c.weight),
                "available": c.available,
            }
            for c in result.components
        ],
    }


def _iso(day: date | None) -> str | None:
    return None if day is None else day.isoformat()


def _day(raw: str | None) -> date | None:
    return None if raw is None else date.fromisoformat(raw)


def _goal_order(goal: GoalPace) -> tuple:
    return (
        goal.created_month,
        goal.target_month or date.min,
        goal.achieved_month or date.min,
        goal.target,
        goal.saved,
    )


def inputs_from_json(data: dict) -> ScoreInputs:
    """The inputs a snapshot was scored from.

    A v1 snapshot has no goals and no last complete month; those read as
    empty, which is exactly what v1 counted — so every stored snapshot still
    reproduces its stored score.
    """
    stored = data["inputs"]
    return ScoreInputs(
        months=tuple(
            MonthFlow(date.fromisoformat(m["month"]), m["income"], m["expenses"])
            for m in stored["months"]
        ),
        budget_lines=tuple(
            BudgetLineUse(line["slug"], line["allocated"], line["spent"])
            for line in stored["budget_lines"]
        ),
        debt=DebtPicture(**stored["debt"]),
        goals=tuple(
            GoalPace(
                target=g["target"],
                saved=g["saved"],
                created_month=date.fromisoformat(g["created_month"]),
                target_month=_day(g["target_month"]),
                achieved_month=_day(g["achieved_month"]),
            )
            for g in stored.get("goals", [])
        ),
        last_complete_month=_day(stored.get("last_complete_month")),
    )


def result_from_json(
    data: dict, stored_score: int, formula_version: str
) -> ScoreResult:
    """A stored snapshot's result, exactly as it was computed.

    Read back rather than recomputed: a snapshot from an older formula keeps
    the score and parts it had, which `score` under today's formula would not
    reproduce.
    """
    return ScoreResult(
        stored_score,
        formula_version,
        tuple(
            ComponentResult(
                key=c["key"],
                score=None if c["score"] is None else Decimal(c["score"]),
                weight=Decimal(c["weight"]),
                available=c["available"],
            )
            for c in data["components"]
        ),
    )


# --- Reading and keeping ------------------------------------------------------


@dataclass(frozen=True)
class HealthScore:
    learning: LearningState
    # Present once ready — today's, or the one being held (see `missing_month`).
    inputs: ScoreInputs | None
    result: ScoreResult | None
    # Oldest first.
    history: list[HealthScoreSnapshot]
    # Set when the last complete month has no data yet: nothing was scored
    # today, and `result` is the latest snapshot's (None when there is none).
    missing_month: date | None = None
    # The day the held score was computed.
    held_from: date | None = None


async def current_score(
    session: AsyncSession, household_id: uuid.UUID, currency: str, today: date
) -> HealthScore:
    """Today's score, with today's snapshot kept; nothing written while learning.

    **A month not yet imported is not scored.** Most people import a month's
    statement some days after it ends. Until then the last complete month has
    no rows, and scoring it would read as nothing spent (spending within
    budget) and nothing paid (debt missed) — a swing every month-start that
    the daily snapshots would then keep as history. So until that month has
    data, nothing is scored or written, and the latest snapshot is held,
    with `missing_month` saying why (manager decision on review, 2026-10-06).
    """
    learning = await learning_state(session, household_id, currency, today)
    if not learning.ready:
        return HealthScore(learning, None, None, [])

    last = months_back(today.replace(day=1), 2)[0]
    window = months_back(last, SAVINGS_WINDOW_MONTHS)
    figures = await figures_by_month(session, household_id, currency, window)
    last_figures = figures.get(last)
    if last_figures is None:
        history = await _history(session, household_id)
        held = history[-1] if history else None
        if held is None or held.components is None:
            return HealthScore(learning, None, None, history, missing_month=last)
        return HealthScore(
            learning,
            inputs_from_json(held.components),
            result_from_json(held.components, held.score, held.formula_version),
            history,
            missing_month=last,
            held_from=held.scored_on,
        )

    # The budget 4.1 serves for that month, user-set lines and all.
    budget = await budget_for(session, household_id, currency, last, today, ready=True)
    debts, with_minimum, required = (
        await session.execute(
            select(
                func.count(Debt.id),
                func.count(Debt.minimum_payment_minor_units),
                func.coalesce(func.sum(Debt.minimum_payment_minor_units), 0),
            ).where(Debt.household_id == household_id, Debt.currency == currency)
        )
    ).one()

    inputs = ScoreInputs(
        months=tuple(
            MonthFlow(month, f.income, f.expenses)
            for month, f in sorted(figures.items())
        ),
        budget_lines=tuple(
            BudgetLineUse(line.slug, line.allocated, line.spent)
            for line in budget.lines
        ),
        debt=DebtPicture(
            debts=int(debts),
            debts_with_minimum=int(with_minimum),
            required=int(required),
            paid=last_figures.debt_paid,
        ),
        goals=await _goal_paces(session, household_id),
        last_complete_month=last,
    )
    result = score(inputs)
    if result.score is not None:
        await _keep(session, household_id, today, inputs, result)
    else:
        # Nothing scorable now. A snapshot kept earlier today would otherwise
        # sit in the history saying today scored what today no longer does.
        await session.execute(
            delete(HealthScoreSnapshot).where(
                HealthScoreSnapshot.household_id == household_id,
                HealthScoreSnapshot.scored_on == today,
            )
        )
    return HealthScore(learning, inputs, result, await _history(session, household_id))


async def _goal_paces(
    session: AsyncSession, household_id: uuid.UUID
) -> tuple[GoalPace, ...]:
    """The household's goals (5.1) as goal completion reads them.

    Months are taken in UTC, the convention every "this month" here follows.
    """
    goals = await session.scalars(select(Goal).where(Goal.household_id == household_id))
    return tuple(
        GoalPace(
            target=goal.target_minor_units,
            saved=goal.saved_minor_units,
            created_month=goal.created_at.astimezone(UTC).date().replace(day=1),
            target_month=goal.target_date.replace(day=1) if goal.target_date else None,
            achieved_month=goal.achieved_at.replace(day=1)
            if goal.achieved_at
            else None,
        )
        for goal in goals.all()
    )


async def _history(
    session: AsyncSession, household_id: uuid.UUID
) -> list[HealthScoreSnapshot]:
    """The last snapshots, oldest first."""
    newest_first = await session.scalars(
        select(HealthScoreSnapshot)
        .where(HealthScoreSnapshot.household_id == household_id)
        .order_by(HealthScoreSnapshot.scored_on.desc())
        .limit(HISTORY_LENGTH)
        # Today's row may already be in the session from an earlier read; it
        # must show what was just upserted.
        .execution_options(populate_existing=True)
    )
    return list(reversed(newest_first.all()))


async def _keep(
    session: AsyncSession,
    household_id: uuid.UUID,
    today: date,
    inputs: ScoreInputs,
    result: ScoreResult,
) -> None:
    """Upsert today's snapshot. Only today's: the key is the date, so a past
    day's row can be read but is never the one this writes."""
    breakdown = to_json(inputs, result)
    statement = pg_insert(HealthScoreSnapshot.__table__).values(
        id=uuid.uuid4(),
        household_id=household_id,
        scored_on=today,
        score=result.score,
        formula_version=result.formula_version,
        components=breakdown,
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["household_id", "scored_on"],
            set_={
                "score": statement.excluded.score,
                "formula_version": statement.excluded.formula_version,
                "components": statement.excluded.components,
                "updated_at": func.now(),
            },
        )
    )
