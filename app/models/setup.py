"""What the financial setup wizard collects (PRD F1).

Typed in right after signup, before any statement exists, and **skippable at
every step** — which is why almost everything here is nullable or may simply be
absent. These figures seed the first dashboard (M4) so a new account is useful
before its first upload.

Debts are not here: they go in `debt` (app/models/planning.py), which already
holds exactly what the wizard asks and is what the payoff optimizer reads.
`debt.entered_via_setup` marks the rows the wizard owns.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import (
    HouseholdScopedMixin,
    TimestampMixin,
    UUIDMixin,
    currency_check,
    money_amount,
    money_currency,
)

__all__ = ["FinancialProfile", "Obligation", "Investment"]


class FinancialProfile(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """One row per household: the wizard's single-value answers and its state.

    Income is nullable because that step, like every other, may be skipped.

    Status is **derived** from the two timestamps rather than stored as its own
    column, so a status can never disagree with the timestamps it summarises.

    `currency` records what the amounts here are denominated in, taken from the
    household's region when they were saved. A later region change does not
    reinterpret them (PRD §4.6: historical records keep the currency they were
    recorded in).
    """

    __tablename__ = "financial_profile"
    __table_args__ = (UniqueConstraint("household_id"), currency_check())

    monthly_income_minor_units: Mapped[int | None] = money_amount(
        "monthly_income_minor_units", nullable=True
    )
    currency: Mapped[str] = money_currency()

    setup_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    setup_skipped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Obligation(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """A recurring monthly commitment — rent, a phone plan, insurance."""

    __tablename__ = "obligation"
    __table_args__ = (currency_check(),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    monthly_amount_minor_units: Mapped[int] = money_amount(
        "monthly_amount_minor_units", nullable=False
    )
    currency: Mapped[str] = money_currency()

    # The order the user typed. Rows are rewritten on every save and share one
    # created_at (Postgres now() is transaction time), so without this the list
    # would come back alphabetically and reshuffle under the user mid-wizard.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Investment(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """An investment holding, **amount only** in v1.

    No ticker, no quantity, no market data: the wizard asks what it is worth,
    not what it consists of. Real holdings arrive with F16 (Phase 3).
    """

    __tablename__ = "investment"
    __table_args__ = (currency_check(),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    amount_minor_units: Mapped[int] = money_amount("amount_minor_units", nullable=False)
    currency: Mapped[str] = money_currency()

    # The order the user typed — see Obligation.position.
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
