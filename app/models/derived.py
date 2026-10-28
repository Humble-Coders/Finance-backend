"""Server-computed outputs: health scores, chat history and AI budget suggestions.

Both are authoritative artefacts of the backend. Clients display them and never
recompute them (PRD §4.1).
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import BigInteger, Date, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
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

__all__ = [
    "AiBudgetSuggestion",
    "ChatConversation",
    "HealthScoreSnapshot",
    "LedgerState",
    "MonthlyCategoryTotal",
    "PlanDiagnostic",
]


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


class AiBudgetSuggestion(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """What the AI suggested for a month's budget, after the server checked it.

    `kind` is `setup` (the first budget, from the wizard's answers) or `review`
    (once a month, from how the months before it went). One row per household,
    month and kind: asking again returns what was stored, which is what keeps
    the cost to a few calls a month (`attempts` counts the setup's retries).

    `lines` holds the checked figures, never the model's raw answer:
    `[{"category_id", "slug", "amount_minor_units", "reason"}]`. Reasons and
    `advice` carry no figures — every number shown comes from the server.
    """

    __tablename__ = "ai_budget_suggestion"
    __table_args__ = (
        Index(
            "uq_ai_budget_suggestion_household_month_kind",
            "household_id",
            "month",
            "kind",
            unique=True,
        ),
        currency_check(),
    )

    month: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # What every amount in `lines` and `savings_minor_units` is in.
    currency: Mapped[str] = money_currency()
    lines: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    savings_minor_units: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    advice: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class PlanDiagnostic(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """One run of a Your-plan diagnostic: the figures the server worked out,
    and what the AI said about them.

    `kind` is `allocation` (pay debt, fund goals, or keep in savings?) or
    `invest` (should I invest, and how much?). `fingerprint` hashes the
    figures the AI was given: the same figures again return this run instead
    of asking again, and a month allows `MAX_RUNS_A_MONTH` fresh runs.
    `figures` holds minor units in `currency`; `advice` holds words only.
    """

    __tablename__ = "plan_diagnostic"
    __table_args__ = (
        Index(
            "ix_plan_diagnostic_household_kind_month", "household_id", "kind", "month"
        ),
        currency_check(),
    )

    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    month: Mapped[date] = mapped_column(Date, nullable=False)
    currency: Mapped[str] = money_currency()
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # What the person typed: {"savings_target": int, "savings_now": int}.
    inputs: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    figures: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    advice: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)


class MonthlyCategoryTotal(UUIDMixin, HouseholdScopedMixin, Base):
    """A household's money for one month, category and direction, summed.

    What the AI coach reads for any period question: ten years is about a
    thousand of these rows instead of tens of thousands of transactions
    (#83). Derived, never edited — `app/services/rollups.py` deletes and
    rebuilds a month's rows whenever the ledger under it changes.

    Kept for **every** currency a household holds; a reader filters to the
    household's own, as `countable` does, so a change of country needs no
    rebuild. Unresolved suspected duplicates are never in it. One-offs are in
    `total_minor_units`, and also in `one_off_minor_units` so an answer can say
    "including a $1,200 one-off".
    """

    __tablename__ = "monthly_category_total"
    __table_args__ = (
        # Unfiled rows (no category) are one row a month too: NULL is folded
        # to the zero UUID, which works on any Postgres (NULLS NOT DISTINCT
        # needs 15).
        Index(
            "uq_monthly_category_total_key",
            "household_id",
            "currency",
            "month",
            text("coalesce(category_id, '00000000-0000-0000-0000-000000000000')"),
            "direction",
            unique=True,
        ),
        currency_check(),
    )

    currency: Mapped[str] = money_currency()
    # The first day of the month.
    month: Mapped[date] = mapped_column(Date, nullable=False)
    # NULL: not filed yet. RESTRICT, not SET NULL: a category deleted from
    # under these rows would leave them filed wrongly; a future delete must
    # rebuild the household's totals first.
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("category.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # Copied so a read needs no join; NULL with `category_id`.
    category_slug: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # `debit` or `credit`, as on the transaction.
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    total_minor_units: Mapped[int] = money_amount("total_minor_units", nullable=False)
    one_off_minor_units: Mapped[int] = money_amount(
        "one_off_minor_units", nullable=False
    )
    transaction_count: Mapped[int] = mapped_column(Integer, nullable=False)


class LedgerState(UUIDMixin, TimestampMixin, HouseholdScopedMixin, Base):
    """One row per household: a number that goes up whenever its ledger does.

    Anything cached from the ledger — a suggested answer (6.2), a chat answer
    (6.4) — is keyed by `ledger_version`, so a write invalidates it without
    anyone having to remember to.
    """

    __tablename__ = "ledger_state"
    __table_args__ = (Index("uq_ledger_state_household", "household_id", unique=True),)

    ledger_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
