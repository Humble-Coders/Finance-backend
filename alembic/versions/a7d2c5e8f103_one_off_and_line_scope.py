"""One-off purchases, and budget lines changed for one month only

Revision ID: a7d2c5e8f103
Revises: e1c5b7a9d204
Create Date: 2026-10-27

A transaction marked one-off still counts in its month but is left out of the
history future budgets are suggested from. A hand-set budget line can be
changed for its month alone: `carries_forward` says whether it starts the
months after, and `carry_minor_units` with what amount when that differs
from the month's own.
"""

import sqlalchemy as sa

from alembic import op

revision = "a7d2c5e8f103"
down_revision = "e1c5b7a9d204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "transaction",
        sa.Column(
            "is_one_off", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "budget_line",
        sa.Column(
            "carries_forward",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "budget_line", sa.Column("carry_minor_units", sa.BigInteger(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("budget_line", "carry_minor_units")
    op.drop_column("budget_line", "carries_forward")
    op.drop_column("transaction", "is_one_off")
