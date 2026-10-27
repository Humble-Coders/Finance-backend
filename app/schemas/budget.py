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

from pydantic import BaseModel, Field


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
    # The AI's suggestion for the line this month, checked by the server, and
    # why. Null without one. Advisory: nothing changes until the person uses it.
    ai_suggested: str | None = None
    ai_reason: str | None = None
    # The part of `spent` marked one-off; null when none of it is.
    one_off: str | None = None
    # Set by hand for this month alone: the months after start elsewhere.
    this_month_only: bool = False


class UnbudgetedOut(BaseModel):
    category_id: uuid.UUID
    slug: str
    name: str
    spent: str
    ai_suggested: str | None = None
    ai_reason: str | None = None
    one_off: str | None = None


class AiLineOut(BaseModel):
    """An AI suggestion for a category with no line and no spending yet."""

    category_id: uuid.UUID
    slug: str
    name: str
    amount: str
    reason: str | None = None


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
    # The savings target not yet moved to savings.
    savings_due: str | None = None
    # month_left + income_expected - commitments_due - budget_rest - savings_due:
    # what will be free for goals or more savings.
    projected_left: str | None = None
    # Money moved to savings this month, less any taken back out.
    month_saved: str = "0.00"
    # month_net - month_saved: income less spending less savings.
    month_left: str = "0.00"
    # Expected income less every line but savings: the room for a target.
    savings_room: str = "0.00"
    # The category a savings target is set on (`PUT .../lines/{id}`).
    savings_category_id: uuid.UUID | None = None
    # The month's AI suggestions: "review" (monthly) or "setup" (the first
    # budget), else null. The lines carry theirs; these are the rest.
    ai_kind: str | None = None
    ai_extra: list[AiLineOut] = []
    ai_savings: str | None = None
    # A few sentences, never a figure: what went over, what was saved, and
    # where the money left over is best put next.
    ai_advice: str | None = None
    # The app may ask for this month's review (`POST .../ai/review`).
    ai_review_due: bool = False


class AiProposalLineOut(BaseModel):
    category_id: uuid.UUID
    slug: str
    name: str
    # need, want, or debt (the minimum payments, set by the budget's own rule).
    kind: str
    amount: str
    # What the person's commitments in the category already hold.
    committed: str
    # What the person usually spends here a month — the median of their last
    # three months — or null with no history (a new account).
    usual: str | None = None
    reason: str | None = None


class AiProposalOut(BaseModel):
    """The first budget the AI suggests, every figure checked by the server.

    The lines, the Debt line and savings add up to `income` exactly.
    """

    month: date
    currency: str
    income: str
    lines: list[AiProposalLineOut]
    savings: str
    needs: str
    wants: str
    # Shares of income, whole percentages, for the 50/30/20 picture.
    needs_percent: int
    wants_percent: int
    savings_percent: int
    note: str | None = None
    attempts_left: int


class AiAcceptLineIn(BaseModel):
    category_id: uuid.UUID
    amount: str


class AiAcceptIn(BaseModel):
    """The budget the person starts with: the AI's lines as they edited them."""

    lines: list[AiAcceptLineIn] = Field(default_factory=list, max_length=40)
    # The savings target; null or absent sets none.
    savings: str | None = None


class BudgetLineIn(BaseModel):
    # A decimal string, like every amount the API accepts. Parsed by
    # `app/core/money.py`, which refuses more precision than the currency has.
    amount: str
    # True: this month only — the months after keep what they would have had.
    this_month_only: bool = False


class CategoryTipOut(BaseModel):
    category_id: uuid.UUID
    slug: str
    tip: str


class SpendingCheckOut(BaseModel):
    """A month's spending against income under 50/30/20, adjusted, and the
    AI's advice on it. Every figure is the server's; the advice is words."""

    month: date
    currency: str
    month_ended: bool
    income: str
    needs: str
    wants: str
    saved: str
    # Spending with no category yet, in neither share.
    unsorted: str
    # Whole percentages of income, and what the rule aims for.
    needs_percent: int
    wants_percent: int
    saved_percent: int
    needs_aim_percent: int
    wants_aim_percent: int
    savings_aim_percent: int
    needs_over: bool
    wants_over: bool
    savings_short: bool
    # The month's last check; all empty until one has been run.
    headline: str | None = None
    tips: list[str] = []
    categories: list[CategoryTipOut] = []
    made_at: str | None = None
    # The figures have changed since that check: running again is worth it.
    stale: bool = False
    # Whether this answer came from storage rather than a fresh run.
    stored: bool = True
    runs_left: int
