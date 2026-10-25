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


class DiagnosticIn(BaseModel):
    """The person's own savings figures, as decimal strings. Left out, the
    ones they ran with last are used (else the plan's own buffer)."""

    savings_target: str | None = None
    savings_now: str | None = None


class DiagnosticGoalOut(BaseModel):
    id: uuid.UUID
    name: str
    amount: str


class DiagnosticStepOut(BaseModel):
    # savings, goals, debt or invest
    to: str
    why: str


class DiagnosticOut(BaseModel):
    """One diagnostic run: the server's figures, and the AI's words about them.

    Figures are the server's (decimal strings); the words carry no figures.
    `stored` is true when nothing had changed and the last run was returned.
    """

    kind: str
    made_at: str
    stored: bool
    currency: str
    # Both kinds.
    leftover: str
    savings_target: str
    savings_now: str
    savings_gap: str
    to_savings: str
    to_goals: str
    to_debt: str
    to_invest: str
    rate_line_percent: str | None = None
    assumed_return_percent: str | None = None
    # allocation
    goals: list[DiagnosticGoalOut] = []
    debt_first: str | None = None
    debt_first_rate_percent: str | None = None
    interest_saved: str | None = None
    debt_free: str | None = None
    debt_free_minimums: str | None = None
    per_100: str | None = None
    debt_per_100: str | None = None
    invest_per_100: str | None = None
    # invest
    buffer_full: bool | None = None
    expensive_debts: list[str] = []
    goals_covered: bool | None = None
    existing_investments: str | None = None
    disclaimer: str | None = None
    # The AI's words.
    headline: str | None = None
    steps: list[DiagnosticStepOut] = []
    note: str | None = None
    why: str | None = None
    where: str | None = None
    next: str | None = None


class DiagnosticsOut(BaseModel):
    """The last run of each, the savings figures to start the form with, and
    how many fresh runs are left this month."""

    allocation: DiagnosticOut | None = None
    invest: DiagnosticOut | None = None
    savings_target: str
    savings_now: str
    runs_left: dict[str, int]
