"""Request and response shapes for the financial setup wizard.

Amounts are **decimal strings** in both directions (PRD §4.4). Pydantic checks
shape here; amounts are converted — and refused — in the service, where the
household's currency is known, so an error can name the exact field.

`income` and `monthly_expense` are the mandatory pair the onboarding rule gates
on (2.5); everything else is optional and editable later from the profile.
"""

from __future__ import annotations

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
    """Everything the wizard holds.

    `income` and `monthly_expense` are the **mandatory** pair: the onboarding
    rule reports `financial_setup` until both are stored (PRD §9). The lists are
    optional and may stay empty — they are editable later from the profile.

    **A save replaces what the wizard owns**, which is what makes it resumable:
    the same call repeated leaves the same rows. So send the whole wizard every
    time, including the figures already entered — omitting `income` clears it,
    and that re-raises the onboarding gate.

    Nullable rather than required, because a save arrives after each step and
    the later steps are reached before the figures have both been typed.
    """

    income: str | None = None
    monthly_expense: str | None = None
    debts: list[DebtIn] = Field(default_factory=list, max_length=MAX_ITEMS)
    investments: list[InvestmentIn] = Field(default_factory=list, max_length=MAX_ITEMS)
    obligations: list[ObligationIn] = Field(default_factory=list, max_length=MAX_ITEMS)


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
    """What is saved.

    No status field: the mandatory half is gated by the onboarding rule, and for
    the optional half a skipped answer and an unasked one are the same fact — no
    row — so absence is the record (#29).
    """

    # What the amounts are denominated in, so the wizard can render them without
    # a second call to /capabilities.
    currency: str
    income: str | None
    monthly_expense: str | None
    debts: list[DebtOut]
    investments: list[InvestmentOut]
    obligations: list[ObligationOut]
