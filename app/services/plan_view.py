"""Gathering a household's plan (#76) — the I/O half of `plan`.

Reads what the arithmetic needs — the budget's money left over, the person's
choices (or suggestions until they choose), their goals and their debts — and
hands it to `plan.split` and `plan.schedule`. Nothing here computes a figure of
its own.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import GoalKind, PlanStrategy
from app.models.planning import Debt, Goal, PlanPreference
from app.models.setup import FinancialProfile
from app.services import goals as goal_service
from app.services import plan
from app.services.budget import budget_for
from app.services.dashboard import figures_by_month, months_back
from app.services.learning import LearningState, learning_state

__all__ = [
    "BUFFER_GOAL_NAME",
    "PlanView",
    "Suggested",
    "build",
    "preference_row",
    "save_preferences",
    "suggested",
    "terms_of",
]

# What the buffer's goal is called when the plan creates it. The app shows it
# by its kind (`emergency_fund`) where it can, so a translated screen need not
# depend on this English name.
BUFFER_GOAL_NAME = "Emergency fund"

# How many complete months the suggested buffer averages over.
BUFFER_MONTHS = 3


@dataclass(frozen=True)
class Suggested:
    buffer: int | None
    goal_share_bps: int
    rate_line_bps: int
    strategy: PlanStrategy


@dataclass(frozen=True)
class PlanView:
    month: date
    currency: str
    learning: LearningState
    leftover: int
    shortfall: int | None
    saved: bool
    suggested: Suggested
    preferences: plan.Preferences
    buffer_goal: Goal | None
    goal_names: dict[uuid.UUID, str]
    debts: list[Debt]
    split: plan.Split
    # The debt this month's extra goes to first; None with no extra.
    focus: uuid.UUID | None
    schedule: plan.Schedule
    minimums_only: plan.Schedule
    other_strategy: plan.Schedule
    # What each goal asks of this month, by priority (the buffer's left out).
    asks: tuple[plan.GoalAsk, ...] = ()


async def preference_row(
    session: AsyncSession, household_id: uuid.UUID
) -> PlanPreference | None:
    return await session.scalar(
        select(PlanPreference).where(PlanPreference.household_id == household_id)
    )


async def suggested(
    session: AsyncSession, household_id: uuid.UUID, currency: str, today: date
) -> Suggested:
    """The starting values, from the household's own figures (manager,
    2026-10-17): a month of its spending, 20%, 6%, avalanche."""
    last_complete = months_back(today.replace(day=1), 2)[0]
    figures = await figures_by_month(
        session, household_id, currency, months_back(last_complete, BUFFER_MONTHS)
    )
    profile = await session.scalar(
        select(FinancialProfile).where(FinancialProfile.household_id == household_id)
    )
    declared = (
        profile.monthly_expense_minor_units
        if profile is not None and profile.currency == currency
        else None
    )
    return Suggested(
        buffer=plan.suggest_buffer(
            [f.expenses for f in figures.values()], declared, currency
        ),
        goal_share_bps=plan.DEFAULT_GOAL_SHARE_BPS,
        rate_line_bps=plan.DEFAULT_RATE_LINE_BPS,
        strategy=PlanStrategy.avalanche,
    )


async def _emergency_goal(
    session: AsyncSession, household_id: uuid.UUID
) -> Goal | None:
    """The household's own emergency-fund goal, the oldest if several."""
    return await session.scalar(
        select(Goal)
        .where(Goal.household_id == household_id, Goal.kind == GoalKind.emergency_fund)
        .order_by(Goal.created_at, Goal.id)
        .limit(1)
    )


async def save_preferences(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    *,
    buffer: int | None,
    goal_share_bps: int,
    rate_line_bps: int,
    strategy: PlanStrategy,
    today: date,
) -> PlanPreference:
    """Keep the person's choices.

    [buffer] is accepted from apps released before 2026-10-28 and ignored:
    Your plan has no buffer step now, and an Emergency fund is a goal the
    person adds themselves (the Goals screen suggests one). Nothing is
    created behind their back.
    """
    del buffer, currency, today
    row = await preference_row(session, household_id)
    goal_id: uuid.UUID | None = None

    if row is None:
        row = PlanPreference(household_id=household_id)
        session.add(row)
    row.buffer_goal_id = goal_id
    row.goal_share_bps = goal_share_bps
    row.rate_line_bps = rate_line_bps
    row.strategy = strategy
    await session.flush()
    return row


def terms_of(debt: Debt) -> plan.DebtTerms:
    return plan.DebtTerms(
        id=debt.id,
        name=debt.name,
        balance=debt.balance_minor_units,
        minimum=debt.minimum_payment_minor_units or 0,
        rate_bps=debt.interest_rate_bps,
        promo_rate_bps=debt.promo_rate_bps,
        promo_ends_on=debt.promo_ends_on,
    )


def _focus(
    terms: list[plan.DebtTerms], month: date, line_bps: int, strategy: str
) -> uuid.UUID | None:
    """The first debt the extra goes to this month, in the strategy's order."""
    attacked = [
        t
        for t in terms
        if t.balance > 0
        and (rate := plan.rate_on(t, month)) is not None
        and rate > line_bps
    ]
    if not attacked:
        return None
    if strategy == plan.SNOWBALL:
        attacked.sort(key=lambda t: (t.balance, -(plan.rate_on(t, month) or 0)))
    else:
        attacked.sort(key=lambda t: (-(plan.rate_on(t, month) or 0), t.balance))
    return attacked[0].id


async def build(
    session: AsyncSession,
    household_id: uuid.UUID,
    currency: str,
    month: date,
    today: date,
    *,
    buffer: tuple[int, int] | None = None,
    goal_share_bps: int | None = None,
) -> PlanView:
    """[month]'s plan. While the household is still learning there is no
    budget yet, so nothing is left over to split: the schedule shows the
    debts at their minimums.

    [buffer] — (target, saved) — puts the person's own savings figures in
    place of the buffer's, for a diagnostic: the same split, their numbers.
    [goal_share_bps] does the same for the goal share — 0 is "debt first".
    """
    month = month.replace(day=1)
    learning = await learning_state(session, household_id, currency, today)
    view = await budget_for(
        session, household_id, currency, month, today, ready=learning.ready
    )
    # What expected income leaves after every line but savings — the room a
    # savings target would come out of. With a shortfall nothing is left.
    leftover = view.savings_room if learning.ready and view.shortfall is None else 0

    row = await preference_row(session, household_id)
    hint = await suggested(session, household_id, currency, today)
    # No buffer step (product decision, 2026-10-28): an Emergency fund is a
    # goal like the others, given its share by priority. Only a diagnostic
    # passes savings figures of its own ([buffer], below).
    buffer_goal = None
    preferences = plan.Preferences(
        buffer_target=None,
        buffer_saved=0,
        goal_share_bps=row.goal_share_bps if row is not None else hint.goal_share_bps,
        rate_line_bps=row.rate_line_bps if row is not None else hint.rate_line_bps,
        strategy=row.strategy.value if row is not None else hint.strategy.value,
    )

    if goal_share_bps is not None:
        preferences = replace(preferences, goal_share_bps=goal_share_bps)
    if buffer is not None:
        preferences = replace(
            preferences, buffer_target=buffer[0] or None, buffer_saved=buffer[1]
        )

    goals = [
        g
        for g in await goal_service.goals_of(session, household_id)
        if (buffer_goal is None or g.id != buffer_goal.id) and g.currency == currency
    ]
    asks = []
    for goal in goals:
        projection = goal_service.project(
            goal.target_minor_units,
            goal.saved_minor_units,
            goal.target_date,
            goal.monthly_contribution_minor_units,
            today,
        )
        if projection.remaining > 0:
            asks.append(
                plan.GoalAsk(
                    goal.id,
                    need=goal_service.monthly_need(goal, projection),
                    remaining=projection.remaining,
                )
            )

    debts = list(
        (
            await session.execute(
                select(Debt)
                .where(Debt.household_id == household_id, Debt.currency == currency)
                .order_by(Debt.entered_via_setup.desc(), Debt.position, Debt.created_at)
            )
        ).scalars()
    )
    terms = [terms_of(debt) for debt in debts]
    divided = plan.split(leftover, preferences, asks, terms, month)
    line, strategy = preferences.rate_line_bps, preferences.strategy
    other = plan.SNOWBALL if strategy == plan.AVALANCHE else plan.AVALANCHE
    return PlanView(
        month=month,
        currency=currency,
        learning=learning,
        leftover=leftover,
        shortfall=view.shortfall if learning.ready else None,
        saved=row is not None,
        suggested=hint,
        preferences=preferences,
        buffer_goal=buffer_goal,
        goal_names={goal.id: goal.name for goal in goals},
        debts=debts,
        split=divided,
        focus=_focus(terms, month, line, strategy) if divided.extra_debt else None,
        schedule=plan.schedule(terms, divided.extra_debt, month, line, strategy),
        minimums_only=plan.schedule(terms, 0, month, line, strategy, rollover=False),
        other_strategy=plan.schedule(terms, divided.extra_debt, month, line, other),
        asks=tuple(asks),
    )
