"""`/plan` — where the money left over goes, debt payoff first (#76).

Amounts are decimal strings and rates percentages ("6.00"), as everywhere at
the boundary (PRD §4.4). Months are "YYYY-MM".
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from app.models.enums import DebtKind, PlanStrategy


class PreferencesIn(BaseModel):
    """The person's choices. `buffer` null means no buffer."""

    buffer: str | None
    goal_share_percent: str
    rate_line_percent: str
    strategy: PlanStrategy


class SuggestedOut(BaseModel):
    # One month of the household's spending; null with nothing to go on.
    buffer: str | None
    goal_share_percent: str
    rate_line_percent: str
    strategy: PlanStrategy


class PreferencesOut(BaseModel):
    # False until the person has saved their choices; the values below are
    # then the suggestions the plan runs on.
    saved: bool
    buffer: str | None
    buffer_goal_id: uuid.UUID | None
    goal_share_percent: str
    rate_line_percent: str
    strategy: PlanStrategy
    suggested: SuggestedOut


class GoalShareOut(BaseModel):
    goal_id: uuid.UUID
    name: str
    amount: str


class SplitOut(BaseModel):
    buffer: str
    buffer_saved: str | None
    buffer_target: str | None
    goals: list[GoalShareOut]
    extra_debt: str
    save_or_invest: str


class PlanDebtOut(BaseModel):
    debt_id: uuid.UUID
    name: str
    kind: DebtKind
    balance: str
    minimum_payment: str | None
    # The rate charged this month: the promotional one while it lasts.
    rate_percent: str | None
    # Position in the payoff order, from 1; null for one that only gets its
    # minimum or has no rate.
    order: int | None
    minimum_only: bool
    missing_rate: bool
    # This month's extra goes here first.
    gets_extra_now: bool
    # "YYYY-MM"; null when never paid off in the plan, or with no rate.
    paid_off: str | None
    never_paid_off: bool
    interest: str


class TotalsOut(BaseModel):
    # Null when a debt is never paid off: there is no finite total.
    total_interest: str | None
    debt_free: str | None
    # Paying only minimums, each debt on its own.
    minimums_only_interest: str | None
    interest_saved: str | None
    other_strategy: PlanStrategy
    # What the other strategy would cost more (negative: less). Null when
    # either run has a debt never paid off.
    other_strategy_difference: str | None


class PlanOut(BaseModel):
    month: str
    currency: str
    # "learning" until the household has a budget: nothing left over to split
    # yet, and the schedule shows the debts at their minimums.
    status: str
    leftover: str
    shortfall: str | None
    preferences: PreferencesOut
    split: SplitOut
    debts: list[PlanDebtOut]
    totals: TotalsOut
