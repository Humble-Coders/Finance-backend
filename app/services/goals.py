"""Savings goals and what each one needs (PRD F5).

Two halves, as in `app/services/health_score.py`:

* **`project`** is the arithmetic: integers and dates in, a projection out, no
  database. Every rule is table-tested, and the same inputs always give the
  same answer. Integer minor units throughout — never a float — and no model
  call: the AI will narrate these numbers (M6), never produce one.
* The persistence around it: create, edit, add money, reorder, delete, and the
  list with its comparison against the month's budget.

**Projection v1 is plain arithmetic** (manager decision, 2026-10-06): no
investment growth and no effect of debts. Both are wanted and undecided, so
the answer says which projection it is (`PROJECTION_VERSION`,
`ASSUMES_GROWTH`) and the math lives in this one function for v2 to replace.

Progress is what the person tells us. We never see an account balance, so
`saved` is set directly or added to; there is no contribution history.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import GoalHorizon, GoalKind, PolicyKind
from app.models.identity import Household
from app.models.planning import Goal
from app.models.platform import CountryPack, DisclaimerVersion
from app.services.budget import budget_for
from app.services.capabilities import resolve
from app.services.learning import learning_state

# --- The projection ----------------------------------------------------------
#
# Changing any rule in `project` means bumping PROJECTION_VERSION, as changing
# the health score's formula means bumping its FORMULA_VERSION: a client, a
# test, or a later explanation must be able to tell which math produced a
# number.

PROJECTION_VERSION = "v1"

# v1 counts only what is put in. Growth on invested savings is a v2 question.
ASSUMES_GROWTH = False

# Goals not yet achieved a household may hold — the setup wizard's ceiling.
GOAL_LIMIT = 20

# The largest amount a BIGINT column holds. Past it, an amount is not one
# anybody means, and without a check it reaches the database as a 500.
MAX_MINOR_UNITS = 2**63 - 1

ACHIEVED = "achieved"
OVERDUE = "overdue"
ON_TRACK = "on_track"
BEHIND = "behind"
OPEN = "open"


@dataclass(frozen=True)
class Projection:
    remaining: int
    # Calendar months from this month through the target month, inclusive;
    # None without a target date, or once it has passed.
    months: int | None
    required_monthly: int | None
    # The first day of the month the contributions cover what remains.
    projected_completion: date | None
    progress_percent: int
    status: str


def _ceil_div(numerator: int, denominator: int) -> int:
    """Integer ceiling, so `denominator × result ≥ numerator` — never short."""
    return -(-numerator // denominator)


def months_through(today: date, target_date: date) -> int:
    """Months from today's month through the target's, counting both.

    A target later this month leaves one month to contribute in; next month,
    two. Calendar months, not 30-day blocks: people save from a pay cycle that
    is monthly.
    """
    return (target_date.year - today.year) * 12 + (target_date.month - today.month) + 1


def _add_months(month: date, count: int) -> date:
    index = month.year * 12 + (month.month - 1) + count
    return date(index // 12, index % 12 + 1, 1)


def project(
    target: int,
    saved: int,
    target_date: date | None,
    monthly_contribution: int | None,
    today: date,
) -> Projection:
    """What a goal needs, and whether it is on pace. Amounts in minor units.

    * `required_monthly` is what is left divided over the months to the target,
      rounded **up** to the minor unit: $1,000.00 over 3 months is $333.34, so
      three payments of it never fall short. A currency without cents has a
      whole unit as its minor unit, so the same ceiling rounds to it.
    * `projected_completion` counts this month as the first contribution.
    * `status`, first match wins: achieved, overdue (the date has passed),
      on track or behind (only with both a date and a contribution to
      compare), else open.
    """
    remaining = max(0, target - saved)
    progress = min(100, (100 * saved) // target) if target > 0 else 100
    overdue = target_date is not None and target_date < today

    months: int | None = None
    required: int | None = None
    if target_date is not None and not overdue:
        months = months_through(today, target_date)
        required = _ceil_div(remaining, months)

    completion: date | None = None
    if monthly_contribution and monthly_contribution > 0 and remaining > 0:
        completion = _add_months(
            today.replace(day=1), _ceil_div(remaining, monthly_contribution) - 1
        )

    if saved >= target:
        status = ACHIEVED
    elif overdue:
        status = OVERDUE
    elif required is not None and monthly_contribution is not None:
        status = ON_TRACK if monthly_contribution >= required else BEHIND
    else:
        status = OPEN

    return Projection(
        remaining=remaining,
        months=months,
        required_monthly=required,
        projected_completion=completion,
        progress_percent=progress,
        status=status,
    )


def monthly_need(goal: Goal, projection: Projection) -> int:
    """What one goal asks of this month, for the budget comparison.

    Its required amount where it has a date still ahead; otherwise what the
    person plans to put in, if they said; otherwise nothing. An overdue goal
    has no months left to divide over, so only a planned contribution counts.
    """
    if projection.status == ACHIEVED:
        return 0
    if projection.required_monthly is not None:
        return projection.required_monthly
    return goal.monthly_contribution_minor_units or 0


# --- Persistence -------------------------------------------------------------


class GoalLimitReached(Exception):
    """A 21st goal not yet achieved."""


def _settle_achievement(goal: Goal, today: date) -> None:
    """Mark the day the target was first reached; clear it if an edit undoes it."""
    if goal.saved_minor_units >= goal.target_minor_units:
        if goal.achieved_at is None:
            goal.achieved_at = today
    else:
        goal.achieved_at = None


async def goals_of(session: AsyncSession, household_id: uuid.UUID) -> list[Goal]:
    """The household's goals in their order: priority, then the older first."""
    result = await session.scalars(
        select(Goal)
        .where(Goal.household_id == household_id)
        .order_by(Goal.priority, Goal.created_at, Goal.id)
        .execution_options(populate_existing=True)
    )
    return list(result.all())


async def owned(
    session: AsyncSession, household_id: uuid.UUID, goal_id: uuid.UUID
) -> Goal | None:
    """The goal, if it is this household's. Another's is answered as missing."""
    return await session.scalar(
        select(Goal)
        .where(Goal.id == goal_id, Goal.household_id == household_id)
        .execution_options(populate_existing=True)
    )


async def create(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    *,
    name: str,
    kind: GoalKind | None,
    horizon: GoalHorizon,
    target: int,
    saved: int,
    target_date: date | None,
    monthly_contribution: int | None,
    today: date,
) -> Goal:
    """A new goal, last in the household's order.

    The limit counts goals not yet achieved, so one that is already achieved
    when created does not count against it.
    """
    if saved < target:
        open_goals = await session.scalar(
            select(func.count(Goal.id)).where(
                Goal.household_id == household_id,
                Goal.saved_minor_units < Goal.target_minor_units,
            )
        )
        if open_goals >= GOAL_LIMIT:
            raise GoalLimitReached
    last = await session.scalar(
        select(func.max(Goal.priority)).where(Goal.household_id == household_id)
    )
    goal = Goal(
        household_id=household_id,
        name=name,
        kind=kind,
        horizon=horizon,
        target_minor_units=target,
        saved_minor_units=saved,
        currency=currency,
        target_date=target_date,
        monthly_contribution_minor_units=monthly_contribution,
        priority=0 if last is None else last + 1,
    )
    _settle_achievement(goal, today)
    session.add(goal)
    await session.flush()
    return goal


async def edit(
    session: AsyncSession, goal: Goal, changes: dict[str, object], today: date
) -> Goal:
    """Apply the fields the person sent, then re-settle `achieved_at`."""
    for field, value in changes.items():
        setattr(goal, field, value)
    _settle_achievement(goal, today)
    await session.flush()
    return goal


async def add_money(
    session: AsyncSession,
    household_id: uuid.UUID,
    goal_id: uuid.UUID,
    amount: int,
    today: date,
) -> Goal | None:
    """Add [amount] to what is saved, in one statement.

    `saved = saved + :amount` in the database, not a read and a write here, so
    two adds at the same moment — two phones, a double tap — both land. The
    same statement stamps `achieved_at` the first time the target is reached:
    `SET` reads the row as it was, so `saved + amount` is the new total.

    None when the goal is not this household's. A total past what the column
    holds is refused rather than overflowing (`AddTooLarge`).
    """
    new_total = Goal.saved_minor_units + amount
    updated = await session.scalar(
        update(Goal)
        .where(
            Goal.id == goal_id,
            Goal.household_id == household_id,
            Goal.saved_minor_units <= MAX_MINOR_UNITS - amount,
        )
        .values(
            saved_minor_units=new_total,
            achieved_at=case(
                (
                    new_total >= Goal.target_minor_units,
                    func.coalesce(Goal.achieved_at, today),
                ),
                else_=Goal.achieved_at,
            ),
            updated_at=func.now(),
        )
        .returning(Goal.id)
    )
    if updated is None:
        if await owned(session, household_id, goal_id) is not None:
            raise AddTooLarge
        return None
    return await owned(session, household_id, goal_id)


class AddTooLarge(Exception):
    """The total would pass what a goal can hold."""


async def reorder(
    session: AsyncSession, household_id: uuid.UUID, ids: list[uuid.UUID]
) -> bool:
    """Set each goal's priority to its position in [ids].

    [ids] must be exactly the household's goals, each once; otherwise nothing
    changes and the answer is False. One statement, so the order is never
    half-applied.
    """
    current = set(
        await session.scalars(select(Goal.id).where(Goal.household_id == household_id))
    )
    if len(ids) != len(set(ids)) or set(ids) != current:
        return False
    if ids:
        await session.execute(
            update(Goal)
            .where(Goal.household_id == household_id)
            .values(
                priority=case(
                    {goal_id: index for index, goal_id in enumerate(ids)}, value=Goal.id
                ),
                updated_at=func.now(),
            )
        )
    return True


async def remove(
    session: AsyncSession, household_id: uuid.UUID, goal_id: uuid.UUID
) -> bool:
    removed = await session.scalar(
        delete(Goal)
        .where(Goal.id == goal_id, Goal.household_id == household_id)
        .returning(Goal.id)
    )
    return removed is not None


# --- The budget comparison ---------------------------------------------------

BUDGET_FEATURE = "auto_budget"

# Why there is no comparison, when there is none.
UNAVAILABLE = "unavailable"
LEARNING = "learning"
NO_SAVINGS_LINE = "no_savings_line"


@dataclass(frozen=True)
class BudgetComparison:
    # What the goals ask of this month together.
    need: int
    # This month's savings line, from 4.1's budget.
    set_aside: int
    # How far the need exceeds the savings line; None when it does not.
    shortfall: int | None


async def compare_with_budget(
    session: AsyncSession,
    household: Household,
    currency: str,
    goals: list[tuple[Goal, Projection]],
    today: date,
) -> tuple[BudgetComparison | None, str | None]:
    """The goals' monthly need against this month's savings line.

    Compare, don't change: goals add no budget lines and move nothing in 4.1.
    Reads the savings line through `budget_for`, which upserts the month's
    budget exactly as `GET /budgets/{month}` would. None, with a reason, when
    there is nothing to compare against. Only goals in the household's
    currency count: a need in one currency set against a line in another is
    not a comparison.
    """
    features = (await resolve(session, household)).features
    feature = features.get(BUDGET_FEATURE)
    if feature is None or not feature.enabled:
        return None, UNAVAILABLE
    learning = await learning_state(session, household.id, currency, today)
    if not learning.ready:
        return None, LEARNING
    view = await budget_for(
        session, household.id, currency, today.replace(day=1), today, ready=True
    )
    if view.savings is None:
        return None, NO_SAVINGS_LINE
    need = sum(
        monthly_need(goal, projection)
        for goal, projection in goals
        if goal.currency == currency
    )
    set_aside = view.savings.allocated
    gap = need - set_aside
    return BudgetComparison(need, set_aside, gap if gap > 0 else None), None


# --- The disclaimer ----------------------------------------------------------


async def regional_disclaimer(
    session: AsyncSession, household: Household
) -> DisclaimerVersion | None:
    """The household's country pack's regional disclaimer, if it has one.

    Served rather than built into the apps (PRD §4.6): wording a regulator may
    need changed must be changeable without an app release.
    """
    if not household.country_code:
        return None
    pack = await session.scalar(
        select(CountryPack).where(CountryPack.country_code == household.country_code)
    )
    if pack is None or not pack.disclaimer_version:
        return None
    return await session.scalar(
        select(DisclaimerVersion).where(
            DisclaimerVersion.country_code == pack.country_code,
            DisclaimerVersion.version == pack.disclaimer_version,
            DisclaimerVersion.kind == PolicyKind.regional_disclaimer,
        )
    )
