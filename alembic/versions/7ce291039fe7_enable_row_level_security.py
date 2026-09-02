"""enable row level security

Revision ID: 7ce291039fe7
Revises: fb76533e5c83
Create Date: 2026-09-02 10:04:28.579289
"""
from alembic import op
import sqlalchemy as sa


revision = '7ce291039fe7'
down_revision = 'fb76533e5c83'
branch_labels = None
depends_on = None


TABLES = (
    "household",
    "user",
    "user_identity",
    "account",
    "document_upload",
    "transaction",
    "category",
    "category_correction",
    "budget",
    "budget_line",
    "goal",
    "debt",
    "health_score_snapshot",
    "chat_conversation",
    "subscription_entitlement",
    "country_pack",
    "feature_availability",
    "disclaimer_version",
)


def upgrade() -> None:
    """Enable RLS everywhere, with no policies.

    Defence in depth (PRD §4.2). Only the backend touches these tables, and it
    connects as a role that bypasses RLS — so this changes nothing about how the
    application works. What it does is make the database refuse every other
    connection by default: if the anon key were ever mistakenly pointed at these
    tables, or a key leaked, the answer is no rows rather than all of them.

    No policies are added deliberately. A table with RLS enabled and no policy
    denies everything, which is the correct posture until some non-bypassing
    role legitimately needs access.
    """
    for table in TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    for table in reversed(TABLES):
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
