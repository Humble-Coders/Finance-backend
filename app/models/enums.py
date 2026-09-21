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
    "StatementImportStatus",
    "SourceKind",
    "AuthProvider",
    "GoalHorizon",
    "PlanTier",
    "PolicyKind",
    "RegionSource",
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


class StatementImportStatus(enum.Enum):
    """Lifecycle of a statement import.

    Since 2026-09-21 the statement is read on the device and parsed inside the
    request (PRD F2), so an import moves `processing → awaiting_review` in one
    call and `queued` is never written. The value is kept because Postgres
    cannot drop an enum member without rebuilding the type, and rebuilding a
    type to delete a word nobody reads is not worth a production migration.
    """

    queued = "queued"
    processing = "processing"
    awaiting_review = "awaiting_review"
    completed = "completed"
    failed = "failed"


class SourceKind(enum.Enum):
    """How the device got the text out of the statement.

    Worth recording: `pdf_text` comes from the PDF's own text layer and is
    exact, while `ocr` is a reading of pixels and can be wrong in ways that look
    plausible. When a parse turns out badly, this is the first thing to check.
    """

    pdf_text = "pdf_text"
    ocr = "ocr"


class AuthProvider(enum.Enum):
    phone = "phone"
    google = "google"
    apple = "apple"
    # Email + password (manager decision, 2026-09-15 — PRD §9). Postgres cannot
    # drop an enum value, which is why its migration's downgrade rebuilds the type.
    email = "email"


class GoalHorizon(enum.Enum):
    short_term = "short_term"
    long_term = "long_term"


class PlanTier(enum.Enum):
    free = "free"
    personal = "personal"
    family = "family"


class RegionSource(enum.Enum):
    """What set a household's region — recorded in its audit trail."""

    phone = "phone"  # derived from the verified phone number
    user = "user"  # the user chose it (onboarding or settings)


class PolicyKind(enum.Enum):
    """Which kind of legal copy a `disclaimer_version` row holds.

    Consent is logged against the account terms and, separately, against AI
    processing; the regional disclaimer is what a country pack's
    `disclaimer_version` points at. They version independently, so one table
    needs to tell them apart.
    """

    account_terms = "account_terms"
    regional_disclaimer = "regional_disclaimer"
    # Express consent to AI processing of financial data, asked before the first
    # statement import and separate from the account terms (PRD Appendix A.5 #1).
    # Bundling it with the account terms would make it not-express, which is the
    # one thing the requirement is about.
    ai_processing = "ai_processing"
