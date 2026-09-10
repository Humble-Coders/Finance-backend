"""one active entitlement per household

Revision ID: d47f2b91c6ae
Revises: c3a91e7d4b28
Create Date: 2026-09-10 06:04:11.882140
"""
from alembic import op
import sqlalchemy as sa


revision = 'd47f2b91c6ae'
down_revision = 'c3a91e7d4b28'
branch_labels = None
depends_on = None


# SubscriptionEntitlement's docstring has always said "one active row per
# household", but nothing enforced it: ix_entitlement_household_active is a
# plain lookup index, not a unique one. Two active rows were accepted, and
# _plan_for tie-breaks on created_at.desc() — which cannot discriminate, because
# created_at defaults to now() and Postgres now() is transaction start time, so
# rows written in one transaction carry identical timestamps:
#
#     inserted free then family -> _plan_for = free
#     inserted family then free -> _plan_for = family
#
# The household's plan was decided by insertion order. Latent today (nothing
# writes entitlements until M7), which is exactly why it is worth closing now —
# M7 should build on an enforced invariant rather than discover this later.
#
# Partial on is_active: inactive rows are history and many may share a
# household, which is the point of keeping them (see the model docstring).
INDEX = "uq_entitlement_one_active"


def upgrade() -> None:
    # An existing duplicate would fail the CREATE with a bare index error. Name
    # the households instead, so whoever hits it knows what to reconcile.
    conn = op.get_bind()
    duplicates = conn.execute(
        sa.text(
            """
            SELECT household_id, count(*) AS n
            FROM subscription_entitlement
            WHERE is_active
            GROUP BY household_id HAVING count(*) > 1
            """
        )
    ).all()
    if duplicates:
        listed = "; ".join(f"{r.household_id}: {r.n} active rows" for r in duplicates)
        raise RuntimeError(
            "subscription_entitlement holds households with more than one active "
            f"row — reconcile them before this index can be created: {listed}"
        )

    op.create_index(
        INDEX,
        "subscription_entitlement",
        ["household_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="subscription_entitlement")
