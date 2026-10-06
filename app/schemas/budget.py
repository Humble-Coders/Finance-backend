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


class BudgetOut(BaseModel):
    status: Literal["learning", "ready"]
    month: date
    currency: str
    # Present only while learning.
    learning: LearningOut | None = None
    # The rest are present only when ready.
    expected_income: str | None = None
    lines: list[BudgetLineOut] = []
    # The two allocations with rules of their own; null when there is none.
    savings: BudgetLineOut | None = None
    debt: BudgetLineOut | None = None
    total_allocated: str | None = None
    total_spent: str | None = None
    # How far the lines exceed expected income; null when they do not.
    shortfall: str | None = None
    # Spending with no category: reported, never budgeted.
    uncategorised_spent: str | None = None
    uncategorised_count: int | None = None


class BudgetLineIn(BaseModel):
    # A decimal string, like every amount the API accepts. Parsed by
    # `app/core/money.py`, which refuses more precision than the currency has.
    amount: str
