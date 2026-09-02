"""ORM models.

Imported for their side effect of registering with `Base.metadata` — Alembic's
autogenerate sees nothing that has not been imported.
"""

from app.models.categorization import Category, CategoryCorrection
from app.models.derived import ChatConversation, HealthScoreSnapshot
from app.models.enums import (
    AccountKind,
    AuthProvider,
    DocumentStatus,
    GoalHorizon,
    PlanTier,
    TransactionDirection,
    TransactionSource,
)
from app.models.identity import Household, User, UserIdentity
from app.models.money import Account, DocumentUpload, Transaction
from app.models.planning import Budget, BudgetLine, Debt, Goal
from app.models.platform import (
    CountryPack,
    DisclaimerVersion,
    FeatureAvailability,
    SubscriptionEntitlement,
)

__all__ = [
    "Household",
    "User",
    "UserIdentity",
    "Account",
    "Transaction",
    "DocumentUpload",
    "Category",
    "CategoryCorrection",
    "Budget",
    "BudgetLine",
    "Goal",
    "Debt",
    "HealthScoreSnapshot",
    "ChatConversation",
    "SubscriptionEntitlement",
    "CountryPack",
    "FeatureAvailability",
    "DisclaimerVersion",
    "AccountKind",
    "AuthProvider",
    "DocumentStatus",
    "GoalHorizon",
    "PlanTier",
    "TransactionDirection",
    "TransactionSource",
]
