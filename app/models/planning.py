"""Budgets, goals and debts — the forward-looking side of the schema."""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Index, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import (
    HouseholdScopedMixin,
    TimestampMixin,
    UUIDMixin,
    currency_check,
    money_amount,
    money_currency,
)
from app.models.enums import GoalHorizon

__all__ = ["Budget", "BudgetLine", "Goal", "Debt"]


class Budget(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """A month's budget, generated from real spending rather than typed in."""

    __tablename__ = "budget"
    __table_args__ = (
        Index(
            "uq_budget_household_period", "household_id", "period_start", unique=True
        ),
        currency_check(),
    )

    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = money_currency()

    # True when the user has edited the generated allocation, so regeneration
    # does not silently overwrite their intent.
    is_user_modified: Mapped[bool] = mapped_column(nullable=False, default=False)

    lines: Mapped[list[BudgetLine]] = relationship(
        back_populates="budget", cascade="all, delete-orphan"
    )


class BudgetLine(UUIDMixin, TimestampMixin, Base):
    """One category's allocation within a budget."""

    __tablename__ = "budget_line"
    __table_args__ = (
        Index(
            "uq_budget_line_budget_category", "budget_id", "category_id", unique=True
        ),
        currency_check(),
    )

    budget_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("budget.id", ondelete="CASCADE"),
        nullable=False,
    )
    category_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("category.id", ondelete="CASCADE"),
        nullable=False,
    )

    allocated_minor_units: Mapped[int] = money_amount(
        "allocated_minor_units", nullable=False
    )
    currency: Mapped[str] = money_currency()

    budget: Mapped[Budget] = relationship(back_populates="lines")


class Goal(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """A savings target, short- or long-term."""

    __tablename__ = "goal"
    __table_args__ = (currency_check(),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    horizon: Mapped[GoalHorizon] = mapped_column(
        SAEnum(GoalHorizon, name="goal_horizon"), nullable=False
    )

    target_minor_units: Mapped[int] = money_amount("target_minor_units", nullable=False)
    saved_minor_units: Mapped[int] = money_amount(
        "saved_minor_units", nullable=False, default=0
    )
    currency: Mapped[str] = money_currency()

    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Lower sorts first; user-orderable, since the AI adjusts advice by priority.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    achieved_at: Mapped[date | None] = mapped_column(Date, nullable=True)


class Debt(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """A debt the payoff optimizer will reason about in Phase 2."""

    __tablename__ = "debt"
    __table_args__ = (currency_check(),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("account.id", ondelete="SET NULL"),
        nullable=True,
    )

    balance_minor_units: Mapped[int] = money_amount(
        "balance_minor_units", nullable=False
    )
    minimum_payment_minor_units: Mapped[int | None] = money_amount(
        "minimum_payment_minor_units", nullable=True
    )
    currency: Mapped[str] = money_currency()

    # Basis points (5.25% -> 525). Integer, for the same reason money is:
    # a rate in float drifts once compounded over a repayment schedule.
    interest_rate_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
