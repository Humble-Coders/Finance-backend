"""Server-computed outputs: health scores and chat history.

Both are authoritative artefacts of the backend. Clients display them and never
recompute them (PRD §4.1).
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Date, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import HouseholdScopedMixin, TimestampMixin, UUIDMixin

__all__ = ["HealthScoreSnapshot", "ChatConversation"]


class HealthScoreSnapshot(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """One day's Money Health Score, kept as history so trends are real.

    `formula_version` is stored with the score because the formula will be
    tuned. Without it, a change to the weights would silently rewrite the
    meaning of every past score in the trend chart.
    """

    __tablename__ = "health_score_snapshot"
    __table_args__ = (
        Index(
            "uq_health_score_household_date", "household_id", "scored_on", unique=True
        ),
    )

    scored_on: Mapped[date] = mapped_column(Date, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    formula_version: Mapped[str] = mapped_column(String(32), nullable=False)

    # The component breakdown the score was computed from, so a past score can
    # be explained rather than merely displayed.
    components: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class ChatConversation(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """A chatbot thread.

    Messages are JSONB rather than a child table: they are always read as a
    whole conversation and never queried across users.
    """

    __tablename__ = "chat_conversation"

    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    messages: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    last_message_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
