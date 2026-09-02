"""Shared model foundations.

Three rules from the PRD are enforced here rather than remembered per table:

* **UUID primary keys** (§4.2) — no sequences. A second region means a second
  database, and sequential ids collide across them.
* **Household scoping** (§4.4) — financial records belong to a household, not a
  user. v1 households have one member, but the Family Plan is committed and
  retrofitting would mean migrating every table.
* **Money as integer minor units plus a currency** (§4.4) — never float. The API
  converts to decimal strings; the database only ever sees integers.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

__all__ = [
    "UUIDMixin",
    "currency_check",
    "TimestampMixin",
    "HouseholdScopedMixin",
    "money_amount",
    "money_currency",
    "CURRENCY_LENGTH",
]

CURRENCY_LENGTH = 3


class UUIDMixin:
    """UUID primary key, generated in Python so ids exist before insert."""

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    """Server-side created/updated stamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class HouseholdScopedMixin:
    """Every financial record belongs to exactly one household.

    Declared as a mixin so no table can quietly forget the foreign key or its
    index — every household-scoped query depends on both.
    """

    @declared_attr
    def household_id(cls) -> Mapped[uuid.UUID]:  # noqa: N805
        return mapped_column(
            PgUUID(as_uuid=True),
            ForeignKey("household.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )


def money_amount(name: str = "amount_minor_units", **kwargs) -> Mapped[int]:
    """A money amount in integer minor units.

    BIGINT, and signed — negative values are legitimate (refunds, debts, budget
    overruns), so no non-negative check belongs here.

    Must always be declared alongside its `money_currency` sibling;
    `test_models.py` scans the metadata to enforce that.
    """
    return mapped_column(name, BigInteger, **kwargs)


def money_currency(name: str = "currency", **kwargs) -> Mapped[str]:
    """The ISO 4217 code the paired amount is denominated in.

    Pair with :func:`currency_check` in the table's ``__table_args__`` — a
    CheckConstraint passed to ``mapped_column`` is silently discarded, so the
    constraint has to be declared at table level to exist at all.
    """
    kwargs.setdefault("nullable", False)
    return mapped_column(name, String(CURRENCY_LENGTH), **kwargs)


def currency_check(name: str = "currency") -> CheckConstraint:
    """Reject anything that is not a 3-character ISO 4217 code.

    VARCHAR(3) only caps the length; without this, a 2-letter country code
    typed where a currency belongs ("CA" instead of "CAD") is accepted and
    silently mislabels money.
    """
    return CheckConstraint(
        f"char_length({name}) = {CURRENCY_LENGTH}", name=f"{name}_iso4217_length"
    )
