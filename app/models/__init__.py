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
    PolicyKind,
    RegionSource,
    TransactionDirection,
    TransactionSource,
)
from app.models.identity import (
    ConsentEvent,
    Household,
    HouseholdRegionChange,
    User,
    UserIdentity,
    UserPhoneChange,
)
from app.models.money import Account, DocumentUpload, Transaction
from app.models.planning import Budget, BudgetLine, Debt, Goal
from app.models.platform import (
    CountryPack,
    DisclaimerVersion,
    FeatureAvailability,
    SubscriptionEntitlement,
)
from app.models.setup import FinancialProfile, Investment, Obligation

__all__ = [
    "Household",
    "User",
    "UserIdentity",
    "UserPhoneChange",
    "HouseholdRegionChange",
    "ConsentEvent",
    "Account",
    "Transaction",
    "DocumentUpload",
    "Category",
    "CategoryCorrection",
    "Budget",
    "BudgetLine",
    "Goal",
    "Debt",
    "FinancialProfile",
    "Obligation",
    "Investment",
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
    "PolicyKind",
    "RegionSource",
    "TransactionDirection",
    "TransactionSource",
]
