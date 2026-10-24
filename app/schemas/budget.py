"""The budget payload (ticket #56).

Money crosses as decimal strings, as everywhere else. `allocated` and
`suggested` are separate fields because they can differ: a line the user set
keeps their amount while the suggestion beneath it keeps moving, and a client
showing "suggested $420" beside it needs both.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel


class LearningNeedsOut(BaseModel):
    complete_months: int
    transactions: int


class LearningOut(BaseModel):
    """How far the household is from a first budget (PRD F8)."""

    ready: bool
    complete_months: int
    transactions: int
    needs: LearningNeedsOut


class BudgetLineOut(BaseModel):
    category_id: uuid.UUID
    slug: str
    name: str
    suggested: str
    allocated: str
    is_user_set: bool
    # This month's countable debits in the category, by the dashboard's rule.
    spent: str


class UnbudgetedOut(BaseModel):
    category_id: uuid.UUID
    slug: str
    name: str
    spent: str


class BudgetCommitmentOut(BaseModel):
    """One setup commitment and whether its payment was seen this month.

    `paid_on` null is **not seen**, not unpaid — as on Home, which matches
    payments by the same rule. Clients must word it that way.
    """

    name: str
    expected: str
    due_day: int | None = None
    # The category slug it belongs to; null when it has none.
    category: str | None = None
    paid_on: date | None = None
    paid_amount: str | None = None


class CategorySpendOut(BaseModel):
    # Null for the one entry gathering every uncategorised row.
    category_id: uuid.UUID | None = None
    slug: str | None = None
    name: str | None = None
    spent: str


class BudgetOut(BaseModel):
    status: Literal["learning", "ready"]
    month: date
    currency: str
    # Present only while learning.
    learning: LearningOut | None = None
    expected_income: str
    # While learning, only the lines the user set by hand: nothing is
    # generated until there is enough history to generate from.
    lines: list[BudgetLineOut]
    # The two allocations with rules of their own; null when there is none.
    savings: BudgetLineOut | None = None
    debt: BudgetLineOut | None = None
    total_allocated: str
    total_spent: str
    # How far the lines exceed expected income; null when they do not.
    shortfall: str | None = None
    # Spending with no category: reported, never budgeted.
    uncategorised_spent: str
    uncategorised_count: int
    # Spending in categories this month has no line for, largest first —
    # a first month has nothing to learn from, a one-off has a median of 0.
    unbudgeted: list[UnbudgetedOut] = []
    unbudgeted_spent: str = "0.00"
    # The lines' spending, plus unbudgeted and uncategorised: everything
    # spent against the budget this month.
    spent_overall: str = "0.00"
    # The month's actual income and spending, counted as the dashboard counts
    # them (transfers and savings are neither), and income minus spending.
    month_income: str = "0.00"
    month_spending: str = "0.00"
    month_net: str = "0.00"
    commitments: list[BudgetCommitmentOut] = []
    # True once the month is over: nothing more is to come, and the four
    # figures below are null.
    month_ended: bool = False
    # Where the month's money went, by category, largest first; sums to
    # `month_spending`. Transfers and savings are not in it.
    spending_by_category: list[CategorySpendOut] = []
    # Setup's monthly income not yet seen this month (never below zero).
    income_expected: str | None = None
    # Commitments not seen paid yet.
    commitments_due: str | None = None
    # What the lines have left beyond the commitments still due in them.
    budget_rest: str | None = None
    # month_net + income_expected - commitments_due - budget_rest.
    projected_left: str | None = None


class BudgetLineIn(BaseModel):
    # A decimal string, like every amount the API accepts. Parsed by
    # `app/core/money.py`, which refuses more precision than the currency has.
    amount: str
