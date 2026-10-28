"""Commitments ticked off by hand

Revision ID: b3f8d1a6c240
Revises: a7d2c5e8f103
Create Date: 2026-10-27

A commitment the automatic matching did not find can be ticked off for a
month by naming the transaction that paid it — one already imported, or one
typed in there and then. Keyed by the commitment's name, since setup rewrites
its rows on every save; a transaction pays at most one commitment.
"""

import sqlalchemy as sa

from alembic import op

revision = "b3f8d1a6c240"
down_revision = "a7d2c5e8f103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "commitment_payment",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("household_id", sa.UUID(), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("commitment_key", sa.String(length=255), nullable=False),
        sa.Column("transaction_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_commitment_payment_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transaction.id"],
            name=op.f("fk_commitment_payment_transaction_id_transaction"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commitment_payment")),
    )
    op.create_index(
        op.f("ix_commitment_payment_household_id"),
        "commitment_payment",
        ["household_id"],
    )
    op.create_index(
        "uq_commitment_payment_household_month_key",
        "commitment_payment",
        ["household_id", "month", "commitment_key"],
        unique=True,
    )
    op.create_index(
        "uq_commitment_payment_transaction",
        "commitment_payment",
        ["transaction_id"],
        unique=True,
    )
    # Row level security on, as on every household-scoped table (CLAUDE.md).
    op.execute("ALTER TABLE commitment_payment ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index("uq_commitment_payment_transaction", table_name="commitment_payment")
    op.drop_index(
        "uq_commitment_payment_household_month_key", table_name="commitment_payment"
    )
    op.drop_index(
        op.f("ix_commitment_payment_household_id"), table_name="commitment_payment"
    )
    op.drop_table("commitment_payment")
