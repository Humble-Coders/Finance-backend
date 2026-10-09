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
    "ReviewReason",
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


class ReviewReason(enum.Enum):
    """Why a transaction is waiting for a person.

    `needs_review` says *that* a row needs attention; without this the review
    queue is one undifferentiated list and the user re-checks rows that were
    always fine. Each reason wants a different affordance: a low-confidence row
    needs reading, an unknown category needs picking, a suspected duplicate
    needs comparing against the row it matched.
    """

    low_confidence = "low_confidence"
    unknown_category = "unknown_category"
    suspected_duplicate = "suspected_duplicate"
    # Filed as a transfer because it looks like money moving between two of the
    # household's own accounts (#73) — a card bill paid from chequing. Counted
    # in neither income nor expenses until somebody says otherwise.
    own_transfer = "own_transfer"


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


class GoalKind(enum.Enum):
    """What a goal is for — chosen so an app can show a fitting picture.

    For the picture only. No kind suggests an amount or a date: proposing a
    target ("six months of expenses") is narration, which is the chatbot's
    job (M6), not a default this table should carry. PRD F5's list, plus
    `other`.
    """

    emergency_fund = "emergency_fund"
    vacation = "vacation"
    car = "car"
    electronics = "electronics"
    home = "home"
    retirement = "retirement"
    wealth = "wealth"
    other = "other"


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


class ConsentAction(enum.Enum):
    """One change to a person's consent, as `consent_change` records it (#42).

    PIPEDA, Quebec Law 25 and GDPR Art. 7(3) all give the right to withdraw
    consent at any time. Withdrawal is written down as an event rather than by
    removing the consent it withdraws, so "given, then withdrawn, then given
    again" stays provable after the fact.
    """

    given = "given"
    withdrawn = "withdrawn"
