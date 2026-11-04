"""Split expense (gated by `split_expenses`): a bill divided between people.

Kept apart from the ledger on purpose (product decision, 2026-11-04): the bill
itself reaches the ledger through the card or bank statement it was paid on,
so recording it again here would count it twice. A split is a record of who
owes the person what, and who has paid.

**People who are not users.** `SplitContact` holds names and phone numbers the
person typed in or picked from their phone, so the next split need not ask
again. They are the household's own address book: never shown to anyone else,
never sent to a model, and deleted when the person deletes them. A split keeps
its own copy of each name and number (`SplitParticipant`), so deleting a
contact does not rewrite a bill that was already sent.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
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

__all__ = [
    "Split",
    "SplitContact",
    "SplitItem",
    "SplitItemShare",
    "SplitParticipant",
    "SplitReceiptUsage",
]


class SplitContact(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """Someone the household splits bills with: a name and a WhatsApp number."""

    __tablename__ = "split_contact"
    __table_args__ = (
        Index("uq_split_contact_household_phone", "household_id", "phone", unique=True),
    )

    name: Mapped[str] = mapped_column(String(80), nullable=False)
    # E.164, "+919876543210": what WhatsApp's link needs, digits after the +.
    phone: Mapped[str] = mapped_column(String(20), nullable=False)


class Split(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """One bill, divided. Every amount is minor units in `currency`; the
    participants' shares add up to `total` exactly."""

    __tablename__ = "split"
    __table_args__ = (
        CheckConstraint("mode IN ('equal', 'items')", name="mode_is_known"),
        CheckConstraint("total_minor_units > 0", name="total_is_positive"),
        currency_check(),
        Index("ix_split_household_created", "household_id", "created_at"),
    )

    title: Mapped[str] = mapped_column(String(120), nullable=False)
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    # "equal" — the total shared evenly; "items" — each item to its people,
    # tax and tip following what each person had.
    mode: Mapped[str] = mapped_column(String(8), nullable=False)
    currency: Mapped[str] = money_currency()
    total_minor_units: Mapped[int] = money_amount("total_minor_units")
    tax_minor_units: Mapped[int] = money_amount("tax_minor_units", default=0)
    tip_minor_units: Mapped[int] = money_amount("tip_minor_units", default=0)

    participants: Mapped[list[SplitParticipant]] = relationship(
        back_populates="split",
        cascade="all, delete-orphan",
        order_by="SplitParticipant.position",
    )
    items: Mapped[list[SplitItem]] = relationship(
        back_populates="split",
        cascade="all, delete-orphan",
        order_by="SplitItem.position",
    )


class SplitParticipant(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """One person on a bill — the person themselves (`contact_id` null, no
    phone) or a contact — with their share and whether they have paid."""

    __tablename__ = "split_participant"
    __table_args__ = (
        Index("ix_split_participant_split", "split_id"),
        currency_check(),
    )

    split_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("split.id", ondelete="CASCADE"), nullable=False
    )
    # Null for the person themselves. SET NULL: deleting a contact keeps the
    # bill as it was sent, by the copy below.
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("split_contact.id", ondelete="SET NULL"),
        nullable=True,
    )
    is_self: Mapped[bool] = mapped_column(nullable=False, default=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    # The bill's currency, beside every amount (`test_models.py`).
    currency: Mapped[str] = money_currency()
    share_minor_units: Mapped[int] = money_amount("share_minor_units")
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    split: Mapped[Split] = relationship(back_populates="participants")


class SplitItem(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """One line of an itemised bill, shared evenly by the people it is assigned to."""

    __tablename__ = "split_item"
    __table_args__ = (
        Index("ix_split_item_split", "split_id"),
        CheckConstraint("amount_minor_units > 0", name="amount_is_positive"),
        currency_check(),
    )

    split_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("split.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    currency: Mapped[str] = money_currency()
    amount_minor_units: Mapped[int] = money_amount("amount_minor_units")
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    split: Mapped[Split] = relationship(back_populates="items")
    shares: Mapped[list[SplitItemShare]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )


class SplitItemShare(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """An item assigned to a participant."""

    __tablename__ = "split_item_share"
    __table_args__ = (
        Index("uq_split_item_share", "item_id", "participant_id", unique=True),
    )

    item_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("split_item.id", ondelete="CASCADE"),
        nullable=False,
    )
    participant_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("split_participant.id", ondelete="CASCADE"),
        nullable=False,
    )


class SplitReceiptUsage(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """Receipts read by the AI in a month: the cost guard."""

    __tablename__ = "split_receipt_usage"
    __table_args__ = (
        Index("uq_split_receipt_usage_month", "household_id", "month", unique=True),
    )

    month: Mapped[date] = mapped_column(Date, nullable=False)
    reads: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
