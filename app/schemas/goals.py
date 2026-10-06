"""The goals payload (backend #65, PRD F5).

Money crosses as decimal strings, as everywhere else. Every figure a goal
carries beyond what the person typed — what remains, the monthly need, the
month it completes, progress, status — is the server's (`project`); the apps
display it and never recompute it.
"""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel

from app.models.enums import GoalHorizon, GoalKind

NAME_MAX = 255


class GoalIn(BaseModel):
    # Its length is checked after trimming, by the route: NAME_MAX of name
    # with a space either side is still a name of NAME_MAX.
    name: str
    kind: GoalKind | None = None
    horizon: GoalHorizon
    # Decimal strings, parsed by `app/core/money.py`.
    target: str
    saved: str | None = None
    target_date: date | None = None
    monthly_contribution: str | None = None


class GoalPatch(BaseModel):
    """Any of the fields; one left out is unchanged, one sent as null is cleared
    (`kind`, `target_date`, `monthly_contribution` only)."""

    name: str | None = None
    kind: GoalKind | None = None
    horizon: GoalHorizon | None = None
    target: str | None = None
    saved: str | None = None
    target_date: date | None = None
    monthly_contribution: str | None = None


class AddMoneyIn(BaseModel):
    amount: str


class GoalOrderIn(BaseModel):
    ids: list[uuid.UUID]


class GoalOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: GoalKind | None
    horizon: GoalHorizon
    target: str
    saved: str
    remaining: str
    target_date: date | None
    monthly_contribution: str | None
    # Null without a target date, or once it has passed.
    required_monthly: str | None
    # `YYYY-MM`: the month the contributions cover what remains. Null without a
    # contribution, or with nothing left.
    projected_completion: str | None
    progress_percent: int
    # achieved | overdue | on_track | behind | open
    status: str
    achieved_at: date | None
    priority: int


class GoalsBudgetOut(BaseModel):
    """The goals' monthly need against this month's savings line (4.1)."""

    need: str
    set_aside: str
    # How far the need exceeds the savings line; null when it does not.
    shortfall: str | None


class GoalsOut(BaseModel):
    goals: list[GoalOut]
    # Null when there is nothing to compare against; `budget_reason` says why.
    budget: GoalsBudgetOut | None
    # unavailable | learning | no_savings_line, or null when `budget` is set.
    budget_reason: str | None
    # The regional disclaimer the apps show beside long-term projections; set
    # when any goal is long-term and the region has one.
    disclaimer_version: str | None
    projection_version: str
    # False in v1: projections count only what is put in, no growth.
    assumes_growth: bool
