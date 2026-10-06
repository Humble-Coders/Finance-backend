"""The dashboard payload.

Money crosses as decimal strings, as everywhere else: minor units are an
internal representation and a float would quietly lose a cent.

Expectations and actuals are separate fields on purpose. There is no field
anywhere in here holding the two added together, because adding them is the
double count the whole design avoids — see `app/services/dashboard.py`.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.budget import LearningOut
from app.schemas.health_score import NoticeOut


class FlowOut(BaseModel):
    """A month's movement, and what the wizard said to expect of it."""

    actual: str
    # Null until the wizard has been filled in. A client showing "of X
    # expected" has to hide that half rather than print "of 0".
    expected: str | None = None
    # Last month's actual; null when last month has no rows, so the client
    # omits "vs last month" rather than showing a rise from nothing.
    previous: str | None = None


class StockOut(BaseModel):
    """A balance the user maintains, and this month's movement against it."""

    # From the setup wizard, and only from there: we never observe a
    # portfolio's market value or a loan's outstanding principal.
    balance: str
    # Observed — debits filed under savings or debt_payment this month.
    moved: str
    # Last month's `moved`; null when last month has no rows.
    previous_moved: str | None = None
    # Credits under the same category this month — money taken back out.
    withdrawn: str = "0"


class MatchOut(BaseModel):
    id: uuid.UUID
    occurred_on: date
    amount: str
    description: str | None = None


class CommitmentOut(BaseModel):
    """One obligation from setup, and the payment that settles it.

    `match` being null means **not seen this month**, which is not the same as
    unpaid: a commitment settled in cash, from another account, or written
    differently by the bank will not be found. Clients must word it that way.
    """

    name: str
    expected: str
    match: MatchOut | None = None
    # Day of the month it falls due, when the user said; null otherwise.
    due_day: int | None = None


class MonthPointOut(BaseModel):
    month: date
    # Null for a month with no rows. A zero here would be a fact nobody
    # observed, and a chart must not draw one.
    net: str | None = None
    # The month's parts, null together with `net`.
    income: str | None = None
    expenses: str | None = None
    invested: str | None = None
    withdrawn: str | None = None
    debt_paid: str | None = None


class DayPointOut(BaseModel):
    day: date
    # The running balance at the end of the day: in minus out since the first
    # of the month. The last one equals `net`.
    net: str


class CategorySpendOut(BaseModel):
    """One category's countable debits this month. The uncategorised entry
    has every id and name null."""

    category_id: uuid.UUID | None = None
    slug: str | None = None
    name: str | None = None
    spent: str


class DashboardBudgetLineOut(BaseModel):
    category_id: uuid.UUID
    slug: str
    name: str
    allocated: str
    spent: str
    # How far spending is past the allocation; "0.00" when within it.
    over: str


class DashboardBudgetOut(BaseModel):
    """The month's budget, as `GET /budgets/{month}` serves it (4.1).

    While learning, only the lines the user set by hand; nothing generated.
    """

    status: Literal["learning", "ready"]
    # Present only while learning.
    learning: LearningOut | None = None
    # Spending lines, in the order `/budgets` gives them.
    lines: list[DashboardBudgetLineOut] = []
    # The two allocations with rules of their own, so the totals below add up
    # to lines that are shown.
    savings: DashboardBudgetLineOut | None = None
    debt: DashboardBudgetLineOut | None = None
    total_allocated: str
    total_spent: str
    shortfall: str | None = None


class DashboardScoreOut(BaseModel):
    """The Money Health Score (4.2). It belongs to a day, not a month: the
    current month carries today's, a past month the last snapshot on or
    before its end."""

    status: Literal["learning", "ready"]
    # Present only while learning.
    learning: LearningOut | None = None
    score: int | None = None
    formula_version: str | None = None
    # The day the score shown was computed.
    scored_on: date | None = None
    # The last snapshot in the month before; null if none. The app shows the
    # change from it.
    previous_score: int | None = None
    # Set when the score is held because last month is not imported yet,
    # exactly as `GET /health-score` says it.
    notice: NoticeOut | None = None


class AsOfOut(BaseModel):
    """How current the figures are, household-wide (PRD F12)."""

    latest_transaction_on: date | None = None
    last_import_at: datetime | None = None


class DashboardOut(BaseModel):
    month: date
    currency: str

    # The hero: income minus expenses. Commitments are already inside expenses
    # wherever the statement shows them.
    net: str
    # Null when the previous month has no rows, so the client omits the
    # comparison instead of claiming an infinite rise from nothing.
    previous_net: str | None = None

    income: FlowOut
    expenses: FlowOut
    investments: StockOut
    debts: StockOut

    commitments: list[CommitmentOut]
    trend: list[MonthPointOut]
    # The month day by day, for the home chart; empty for a month with no
    # rows. Defaulted so an older client that never reads it is unaffected.
    daily: list[DayPointOut] = []

    # Rows in this month still waiting on a person. The figures above exclude
    # unresolved suspected duplicates, so this is also the honest caveat on
    # them.
    pending_review: int

    # Added in 4.3. Every one is optional, so an app that predates them
    # decodes the response unchanged.
    spend_by_category: list[CategorySpendOut] = []
    # Null when the household does not have `auto_budget`, or for a month
    # after the current one.
    budget: DashboardBudgetOut | None = None
    # Null when the household does not have `health_score`, or for a month
    # after the current one.
    health_score: DashboardScoreOut | None = None
    as_of: AsOfOut | None = None
    # One "still learning" state for the budget and the score together.
    learning: LearningOut | None = None
