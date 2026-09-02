"""Households, users, and the auth identities that resolve to them."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.base import TimestampMixin, UUIDMixin
from app.models.enums import AuthProvider

if TYPE_CHECKING:
    pass

__all__ = ["Household", "User", "UserIdentity"]


class Household(UUIDMixin, TimestampMixin, Base):
    """The owner of every financial record.

    v1 households have exactly one member. The Family Plan adds a second without
    touching any other table, which is the whole reason this exists now.
    """

    __tablename__ = "household"

    # NULLABLE by design (PRD §4.6): every signup route ends with a verified
    # phone, but Google/Apple users authenticate before providing one, so the
    # region is genuinely unknown for that window. Never guess it.
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True, index=True)

    members: Mapped[list[User]] = relationship(back_populates="household")


class User(UUIDMixin, TimestampMixin, Base):
    """A person. Belongs to exactly one household in v1."""

    __tablename__ = "user"

    household_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("household.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # The Supabase Auth uid. One row per person, whichever provider they used.
    auth_user_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    # UNIQUE where non-null — the verified phone is the canonical identity key
    # (PRD §4.6). Apple's Hide My Email returns a relay address that matches
    # nothing else the person has used, so email cannot serve this purpose; the
    # phone is what stops one person becoming two households.
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True, unique=True)

    email: Mapped[str | None] = mapped_column(String(320), nullable=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    household: Mapped[Household] = relationship(back_populates="members")
    identities: Mapped[list[UserIdentity]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserIdentity(UUIDMixin, TimestampMixin, Base):
    """One sign-in method belonging to a user.

    A person may hold several — phone OTP, Google, Apple — and all of them must
    resolve to the same `user`, or their financial history splits in two.
    """

    __tablename__ = "user_identity"
    __table_args__ = (
        # The same provider account can never map to two users.
        UniqueConstraint("provider", "provider_user_id", name="provider_identity"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[AuthProvider] = mapped_column(
        SAEnum(AuthProvider, name="auth_provider"), nullable=False
    )
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)

    user: Mapped[User] = relationship(back_populates="identities")
