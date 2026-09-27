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
    ReviewReason,
    SourceKind,
    StatementImportStatus,
    TransactionDirection,
    TransactionSource,
)

__all__ = ["Account", "Transaction", "StatementImport", "StatementImportText"]


class Account(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """A logical account — chequing, credit card, loan, investment.

    In v1 these are created from uploaded statements. The external id fields are
    reserved for Phase 2 aggregator linking and stay NULL until then.
    """

    __tablename__ = "account"
    __table_args__ = (
        currency_check(),
        # One "RBC Chequing" per household. A second account for the same real
        # account is a silent dedup hole, because the dedup key below starts
        # with `account_id`: file half a statement under "RBC" and half under
        # "RBC Chequing" and neither half can see the other's duplicates.
        Index("uq_account_household_name", "household_id", "name", unique=True),
    )

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


class StatementImport(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """One statement the user imported.

    **There is no document here, and no column for one.** Since 2026-09-21 the
    statement is read and redacted on the device and only text is sent (PRD F2),
    so `storage_path`, `content_type`, `byte_size` and `deleted_at` were dropped:
    a column describing a file we never receive would tell every future reader
    the opposite of what the product does, and invite someone to start filling
    it.

    There is no filename column either. `statement-jane-smith.pdf` is personal
    data we have no use for, and the cheapest way not to leak something is not
    to ask for it.
    """

    __tablename__ = "statement_import"

    source_kind: Mapped[SourceKind] = mapped_column(
        SAEnum(SourceKind, name="source_kind"), nullable=False
    )
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[StatementImportStatus] = mapped_column(
        SAEnum(StatementImportStatus, name="statement_import_status"),
        nullable=False,
        default=StatementImportStatus.processing,
        index=True,
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    extracted_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="statement_import"
    )
    diagnostic_text: Mapped[StatementImportText | None] = relationship(
        back_populates="statement_import", cascade="all, delete-orphan", uselist=False
    )


class StatementImportText(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """Redacted statement text the user explicitly asked us to keep.

    The **only** thing retained from an import beyond the transactions
    themselves, and only when the user opts in after an import failed or came
    back mostly flagged (manager decision, 2026-09-21). The review queue tells us
    *that* a parse went wrong; only this says why.

    Its own table rather than a column on `statement_import`, for two reasons
    that both matter more than tidiness: a query for imports can never load it by
    accident, and disposing of it is one `DELETE` rather than an `UPDATE` over a
    table we read constantly.

    `expires_at` is 30 days out and enforced by the parse path itself — every
    parse request clears what has expired. A retention rule that depends on a
    scheduled job nobody watches is how "we delete it after 30 days" becomes
    false without anyone noticing.
    """

    __tablename__ = "statement_import_text"

    statement_import_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("statement_import.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # Redacted on the device before it was ever sent. Never the document.
    text: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    statement_import: Mapped[StatementImport] = relationship(
        back_populates="diagnostic_text"
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
            "occurrence",
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
    statement_import_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("statement_import.id", ondelete="SET NULL"),
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

    # Which of N identical transactions this is — the 1st $5.25 Tim Hortons on
    # the 2nd, the 2nd, the 3rd.
    #
    # Without it the dedup key cannot tell a real repeat purchase from a
    # re-imported one: two coffees at the same shop, same day, same price
    # produce byte-identical rows, and so does importing one coffee twice. The
    # difference is context, not content — two identical lines *within* one
    # statement are two purchases; the same line appearing in a *later* import
    # is a duplicate. Numbering occurrences per import is what lets the database
    # express both, so a re-import collides exactly and a genuine third coffee
    # does not.
    #
    # Defaults to 1 on purpose: "the first of its group" is the right answer for
    # any row that has no opinion — a manually typed transaction, a future
    # aggregator feed, a fixture checking something else. The import path always
    # sets it explicitly.
    occurrence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    # Set by extraction; low-confidence rows go to the user review queue (F2).
    extraction_confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    needs_review: Mapped[bool] = mapped_column(
        nullable=False, default=False, index=True
    )
    review_reason: Mapped[ReviewReason | None] = mapped_column(
        SAEnum(ReviewReason, name="review_reason"), nullable=True
    )
    # What a suspected duplicate matched. Flagging a row without saying what it
    # collided with leaves the review screen asking a question it cannot show
    # the evidence for — and makes a merge impossible.
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("transaction.id", ondelete="SET NULL"),
        nullable=True,
    )

    account: Mapped[Account] = relationship(back_populates="transactions")
    statement_import: Mapped[StatementImport | None] = relationship(
        back_populates="transactions"
    )
