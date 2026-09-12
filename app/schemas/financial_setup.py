"""Request and response shapes for the financial setup wizard.

Amounts are **decimal strings** in both directions (PRD §4.4). Pydantic checks
shape here; amounts are converted — and refused — in the service, where the
household's currency is known, so an error can name the exact field.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

__all__ = [
    "DebtIn",
    "DebtOut",
    "FinancialSetupIn",
    "FinancialSetupOut",
    "InvestmentIn",
    "InvestmentOut",
    "ObligationIn",
    "ObligationOut",
]

MAX_ITEMS = 20
NAME_MAX = 255


class DebtIn(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    balance: str
    minimum_payment: str | None = None
    # A percentage as typed, e.g. "5.25"; stored as basis points.
    interest_rate_percent: str | None = None


class InvestmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    amount: str


class ObligationIn(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    monthly_amount: str


class FinancialSetupIn(BaseModel):
    """Everything the wizard holds. Every part is optional — it is skippable.

    Sent after each step and again at the end with `finished: true`, which is
    what makes the wizard resumable: each save replaces what the wizard owns.
    """

    income: str | None = None
    debts: list[DebtIn] = Field(default_factory=list, max_length=MAX_ITEMS)
    investments: list[InvestmentIn] = Field(default_factory=list, max_length=MAX_ITEMS)
    obligations: list[ObligationIn] = Field(default_factory=list, max_length=MAX_ITEMS)
    finished: bool = False


class DebtOut(BaseModel):
    name: str
    balance: str
    minimum_payment: str | None
    interest_rate_percent: str | None


class InvestmentOut(BaseModel):
    name: str
    amount: str


class ObligationOut(BaseModel):
    name: str
    monthly_amount: str


class FinancialSetupOut(BaseModel):
    status: Literal["not_started", "skipped", "completed"]
    # What the amounts are denominated in, so the wizard can render them without
    # a second call to /capabilities.
    currency: str
    income: str | None
    debts: list[DebtOut]
    investments: list[InvestmentOut]
    obligations: list[ObligationOut]
