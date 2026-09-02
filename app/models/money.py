"""Accounts, transactions, and the documents transactions come from."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, Text
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
from app.models.enums import (
    AccountKind,
    DocumentStatus,
    TransactionDirection,
    TransactionSource,
)

__all__ = ["Account", "Transaction", "DocumentUpload"]


class Account(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """A logical account — chequing, credit card, loan, investment.

    In v1 these are created from uploaded statements. The external id fields are
    reserved for Phase 2 aggregator linking and stay NULL until then.
    """

    __tablename__ = "account"
    __table_args__ = (currency_check(),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[AccountKind] = mapped_column(
        SAEnum(AccountKind, name="account_kind"), nullable=False
    )
    institution: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Last four digits only. The full number is redacted before anything leaves
    # the extraction pipeline (PRD F2) and must never be stored.
    account_number_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)

    currency: Mapped[str] = money_currency()

    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    transactions: Mapped[list[Transaction]] = relationship(back_populates="account")


class DocumentUpload(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """An uploaded statement, tracked from queue to deletion.

    The source document is deleted once the user confirms the extracted rows, or
    after 72 hours, whichever comes first (PRD F2) — `deleted_at` records that it
    actually happened, so the retention job is auditable rather than assumed.
    """

    __tablename__ = "document_upload"

    storage_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[DocumentStatus] = mapped_column(
        SAEnum(DocumentStatus, name="document_status"),
        nullable=False,
        default=DocumentStatus.queued,
        index=True,
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    extracted_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="document_upload"
    )


class Transaction(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """A single money movement.

    Source-agnostic: uploads, manual entry and (Phase 2) aggregator feeds all
    land here, distinguished by `source` rather than by living in separate
    tables.
    """

    __tablename__ = "transaction"
    __table_args__ = (
        # Re-importing an overlapping statement must not double-count. Enforced
        # by the database, because application-level dedup fails the moment two
        # uploads are processed concurrently.
        Index(
            "uq_transaction_dedup",
            "account_id",
            "occurred_on",
            "amount_minor_units",
            "normalized_description",
            unique=True,
        ),
        Index("ix_transaction_household_occurred", "household_id", "occurred_on"),
        currency_check(),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
    )
    document_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("document_upload.id", ondelete="SET NULL"),
        nullable=True,
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("category.id", ondelete="SET NULL"),
        nullable=True,
    )

    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    amount_minor_units: Mapped[int] = money_amount(nullable=False)
    currency: Mapped[str] = money_currency()
    direction: Mapped[TransactionDirection] = mapped_column(
        SAEnum(TransactionDirection, name="transaction_direction"), nullable=False
    )

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Lower-cased, whitespace- and noise-stripped form of `description`. Part of
    # the dedup key, so it must be produced deterministically.
    normalized_description: Mapped[str] = mapped_column(String(512), nullable=False)
    merchant: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    source: Mapped[TransactionSource] = mapped_column(
        SAEnum(TransactionSource, name="transaction_source"), nullable=False
    )
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Set by extraction; low-confidence rows go to the user review queue (F2).
    extraction_confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    needs_review: Mapped[bool] = mapped_column(
        nullable=False, default=False, index=True
    )

    account: Mapped[Account] = relationship(back_populates="transactions")
    document_upload: Mapped[DocumentUpload | None] = relationship(
        back_populates="transactions"
    )
