"""`/debts` — a household's debts, editable outside the setup wizard (#76)."""

from __future__ import annotations

import uuid
from datetime import date

from pydantic import BaseModel, Field

from app.models.enums import BalanceSource, DebtKind
from app.schemas.financial_setup import NAME_MAX


class DebtIn(BaseModel):
    """A debt added from the Debts screen. Amounts are decimal strings and
    rates percentages as typed ("20.99"), as in the setup wizard."""

    name: str = Field(min_length=1, max_length=NAME_MAX)
    kind: DebtKind = DebtKind.other
    balance: str
    minimum_payment: str | None = None
    interest_rate_percent: str | None = None
    # Both or neither: a promotional rate means nothing without its end.
    promo_rate_percent: str | None = None
    promo_ends_on: date | None = None
    due_day: int | None = Field(default=None, ge=1, le=31)
    account_id: uuid.UUID | None = None


class DebtPatch(BaseModel):
    """Any of the fields sent. Null clears a field that may be empty
    (minimum, rates, promotion, due day, account link); name, kind and
    balance cannot be cleared."""

    name: str | None = Field(default=None, min_length=1, max_length=NAME_MAX)
    kind: DebtKind | None = None
    balance: str | None = None
    minimum_payment: str | None = None
    interest_rate_percent: str | None = None
    promo_rate_percent: str | None = None
    promo_ends_on: date | None = None
    due_day: int | None = Field(default=None, ge=1, le=31)
    account_id: uuid.UUID | None = None


class DebtOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: DebtKind
    balance: str
    minimum_payment: str | None
    interest_rate_percent: str | None
    promo_rate_percent: str | None
    promo_ends_on: date | None
    due_day: int | None
    account_id: uuid.UUID | None
    # The day the balance was true, and where it came from.
    balance_as_of: date | None
    balance_source: BalanceSource
    # Older than `STALE_AFTER_DAYS` and not read from a statement: the screen
    # asks the person to update it.
    balance_stale: bool
    # Typed into the setup wizard, which keeps its name, balance, minimum, rate
    # and due day in step with what it shows.
    from_setup: bool


class DebtsOut(BaseModel):
    currency: str
    debts: list[DebtOut]
