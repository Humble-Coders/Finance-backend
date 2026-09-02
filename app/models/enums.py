"""Enumerated domains, as native Postgres types.

Native enums give the database real integrity rather than a convention. Adding a
value later is `ALTER TYPE ... ADD VALUE`, which Postgres supports; removing one
needs a migration that rewrites the type, so prefer adding.
"""

from __future__ import annotations

import enum

__all__ = [
    "TransactionSource",
    "TransactionDirection",
    "AccountKind",
    "DocumentStatus",
    "AuthProvider",
    "GoalHorizon",
    "PlanTier",
]


class TransactionSource(enum.Enum):
    """Where a transaction came from.

    `aggregator` exists from day one though bank linking is Phase 2 — the point
    of a source-agnostic table is that adding it needs no schema change.
    """

    upload = "upload"
    manual = "manual"
    aggregator = "aggregator"


class TransactionDirection(enum.Enum):
    debit = "debit"
    credit = "credit"


class AccountKind(enum.Enum):
    chequing = "chequing"
    savings = "savings"
    credit_card = "credit_card"
    loan = "loan"
    investment = "investment"
    cash = "cash"


class DocumentStatus(enum.Enum):
    """Lifecycle of an uploaded statement.

    `awaiting_review` is the state the source document is retained for; it is
    deleted on confirmation or after 72 hours, whichever comes first (PRD F2).
    """

    queued = "queued"
    processing = "processing"
    awaiting_review = "awaiting_review"
    completed = "completed"
    failed = "failed"


class AuthProvider(enum.Enum):
    phone = "phone"
    google = "google"
    apple = "apple"


class GoalHorizon(enum.Enum):
    short_term = "short_term"
    long_term = "long_term"


class PlanTier(enum.Enum):
    free = "free"
    personal = "personal"
    family = "family"
