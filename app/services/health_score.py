"""The Money Health Score (PRD F6): one number from 0 to 100, kept as history.

Two halves, as in `app/services/budget.py`:

* **`score`** is the arithmetic. Plain values in, a result out, no database —
  so every rule is table-tested, and the same inputs always give the same
  score. `Decimal` throughout; no float, and no model call anywhere in this
  module (the LLM explains a score in M6; it never produces one).
* **`current_score`** gathers the inputs, scores them, and keeps today's
  snapshot.

**Formula v1** (manager decision 2026-10-04, settling PRD OD4):

* `savings_consistency` (40): over the last three complete months, each
  month's savings rate `net / income`, clamped to 0–20 % and scaled so 20 %
  scores 100; the average of the months that had income.
* `spending_vs_budget` (35): last complete month's spending lines (not
  savings or debt). Within allocation scores 100; over it,
  `max(0, 100 - 100 * overspend / allocated)`. Weighted by allocation.
* `debt_payments` (25): last complete month, `min(100, 100 * paid /
  required)`, `required` being the debts' minimum payments. No debts scores
  100: owing nothing is not a debt problem.

A component that cannot be scored is left out and the weights renormalised
over the rest; with none left there is no score. The score is the weighted
average rounded half-up to an integer.

**History without a scheduler.** There is no worker, so the score is computed
when it is read. Today's snapshot (UTC) is upserted on every read, so an
import later in the day is reflected; a past day's snapshot is never
rewritten. A day nobody opened the app has no snapshot.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.derived import HealthScoreSnapshot
from app.models.planning import Debt
from app.services.budget import budget_for
from app.services.dashboard import figures_by_month, months_back
from app.services.learning import LearningState, learning_state

# --- The formula --------------------------------------------------------------
#
# Changing a weight, the cap, or any rule in `score` means bumping
# FORMULA_VERSION. Every snapshot stores the version it was computed with, so a
# trend never compares two formulas without saying so — and a snapshot whose
# breakdown no longer reproduces its score is how a silent change is caught.

FORMULA_VERSION = "v1"

SAVINGS_CONSISTENCY = "savings_consistency"
SPENDING_VS_BUDGET = "spending_vs_budget"
DEBT_PAYMENTS = "debt_payments"

WEIGHTS: dict[str, int] = {
    SAVINGS_CONSISTENCY: 40,
    SPENDING_VS_BUDGET: 35,
    DEBT_PAYMENTS: 25,
}

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
class ScoreInputs:
    # Complete months in the window that have any countable rows.
    months: tuple[MonthFlow, ...]
    budget_lines: tuple[BudgetLineUse, ...]
    debt: DebtPicture


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


def score(inputs: ScoreInputs) -> ScoreResult:
    """The score for [inputs]: deterministic, and order-independent."""
    raw = {
        SAVINGS_CONSISTENCY: savings_consistency(inputs.months),
        SPENDING_VS_BUDGET: spending_vs_budget(inputs.budget_lines),
        DEBT_PAYMENTS: debt_payments(inputs.debt),
    }
    present = sum(WEIGHTS[key] for key, value in raw.items() if value is not None)
    components = tuple(
        ComponentResult(
            key=key,
            score=value,
            weight=(
                _ZERO
                if value is None
                else Decimal(WEIGHTS[key]) * _HUNDRED / Decimal(present)
            ),
            available=value is not None,
        )
        for key, value in raw.items()
    )
    if present == 0:
        return ScoreResult(None, FORMULA_VERSION, components)
    total = sum(
        (c.score * WEIGHTS[c.key] for c in components if c.score is not None), _ZERO
    ) / Decimal(present)
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


def inputs_from_json(data: dict) -> ScoreInputs:
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
    )


# --- Reading and keeping ------------------------------------------------------


@dataclass(frozen=True)
class HealthScore:
    learning: LearningState
    # Present once ready.
    inputs: ScoreInputs | None
    result: ScoreResult | None
    # Oldest first.
    history: list[HealthScoreSnapshot]


async def current_score(
    session: AsyncSession, household_id: uuid.UUID, currency: str, today: date
) -> HealthScore:
    """Today's score, with today's snapshot kept; nothing written while learning."""
    learning = await learning_state(session, household_id, currency, today)
    if not learning.ready:
        return HealthScore(learning, None, None, [])

    last = months_back(today.replace(day=1), 2)[0]
    window = months_back(last, SAVINGS_WINDOW_MONTHS)
    figures = await figures_by_month(session, household_id, currency, window)
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
    last_figures = figures.get(last)

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
            paid=last_figures.debt_paid if last_figures else 0,
        ),
    )
    result = score(inputs)
    if result.score is not None:
        await _keep(session, household_id, today, inputs, result)
    history = list(
        reversed(
            (
                await session.scalars(
                    select(HealthScoreSnapshot)
                    .where(HealthScoreSnapshot.household_id == household_id)
                    .order_by(HealthScoreSnapshot.scored_on.desc())
                    .limit(HISTORY_LENGTH)
                    # Today's row may already be in the session from an
                    # earlier read; it must show what was just upserted.
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
    )
    return HealthScore(learning, inputs, result, history)


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
