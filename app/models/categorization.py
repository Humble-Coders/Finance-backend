"""Categories and the corrections that teach the categorizer."""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import HouseholdScopedMixin, TimestampMixin, UUIDMixin

__all__ = ["Category", "CategoryCorrection"]


class Category(UUIDMixin, TimestampMixin, Base):
    """A spending category.

    Deliberately **not** household-scoped-by-mixin: `household_id` is NULL for
    the seeded system taxonomy, which is shared by everyone, and set for
    user-defined categories. A NOT NULL mixin cannot express that.
    """

    __tablename__ = "category"
    __table_args__ = (
        # A household cannot define the same category name twice. System rows
        # (household_id NULL) are excluded — Postgres treats NULLs as distinct,
        # so the seed data is unaffected.
        Index("uq_category_household_slug", "household_id", "slug", unique=True),
    )

    household_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("household.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("category.id", ondelete="SET NULL"), nullable=True
    )

    @property
    def is_system(self) -> bool:
        return self.household_id is None


class CategoryCorrection(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """A user overriding the categorizer.

    Corrections are fed back into future categorization prompts **for this
    household only** — never pooled across users (PRD §4.5, Appendix A).
    """

    __tablename__ = "category_correction"

    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("transaction.id", ondelete="SET NULL"), nullable=True
    )
    # The merchant string the correction generalizes from, so it can apply to
    # future transactions rather than only the one that was fixed.
    merchant_pattern: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    predicted_category_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("category.id", ondelete="SET NULL"), nullable=True
    )
    corrected_category_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("category.id", ondelete="CASCADE"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
