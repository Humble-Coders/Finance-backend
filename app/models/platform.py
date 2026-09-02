"""Plans, entitlements and per-country configuration.

Country packs are what make adding a market a **data** operation rather than a
deploy (PRD §4.6). No country-specific behaviour belongs in code.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import (
    TimestampMixin,
    UUIDMixin,
    currency_check,
    money_currency,
)
from app.models.enums import PlanTier

__all__ = [
    "SubscriptionEntitlement",
    "CountryPack",
    "FeatureAvailability",
    "DisclaimerVersion",
]


class SubscriptionEntitlement(UUIDMixin, TimestampMixin, Base):
    """What a household is currently entitled to.

    One active row per household; history is kept by leaving expired rows in
    place rather than updating them.
    """

    __tablename__ = "subscription_entitlement"
    __table_args__ = (
        Index("ix_entitlement_household_active", "household_id", "is_active"),
    )

    household_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("household.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan: Mapped[PlanTier] = mapped_column(
        SAEnum(PlanTier, name="plan_tier"), nullable=False, default=PlanTier.free
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    external_subscription_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )


class CountryPack(UUIDMixin, TimestampMixin, Base):
    """Everything that varies by market, as data.

    Adding a country is an INSERT — no code change, no deploy (PRD §4.6).
    """

    __tablename__ = "country_pack"
    __table_args__ = (currency_check(),)

    country_code: Mapped[str] = mapped_column(String(2), nullable=False, unique=True)
    currency: Mapped[str] = money_currency()
    locale: Mapped[str] = mapped_column(String(16), nullable=False)

    # e.g. ["RRSP", "TFSA", "FHSA"] — educational content about account *types*,
    # never personal tax advice (PRD §1).
    tax_accounts: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    disclaimer_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_launched: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class FeatureAvailability(UUIDMixin, TimestampMixin, Base):
    """Whether a feature is on, per country and/or plan.

    A NULL country or plan means "any" — so one row can express a global
    rollout, and a more specific row can override it.
    """

    __tablename__ = "feature_availability"
    __table_args__ = (
        Index("uq_feature_scope", "feature_key", "country_code", "plan", unique=True),
    )

    feature_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    plan: Mapped[PlanTier | None] = mapped_column(
        SAEnum(PlanTier, name="plan_tier", create_type=False), nullable=True
    )

    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Surfaced to the client so a hidden feature can explain itself
    # ("coming_soon", "not_in_plan", "region_unsupported").
    reason: Mapped[str | None] = mapped_column(String(64), nullable=True)


class DisclaimerVersion(UUIDMixin, TimestampMixin, Base):
    """Versioned legal copy.

    Consent is logged against a version (Appendix A), so the text a user agreed
    to must remain retrievable after it changes.
    """

    __tablename__ = "disclaimer_version"
    __table_args__ = (
        Index("uq_disclaimer_country_version", "country_code", "version", unique=True),
    )

    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    effective_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
