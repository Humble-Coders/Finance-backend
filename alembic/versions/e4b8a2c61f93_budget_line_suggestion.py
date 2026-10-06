"""Budget lines carry their suggestion and whether the user set them

Revision ID: e4b8a2c61f93
Revises: b7d2e19c4a51
Create Date: 2026-10-06

A generated budget is regenerated on every read of a month still running. A
line the user typed must survive that, and must still show the suggestion
moving underneath it, so each line keeps both numbers and a flag saying which
one is in charge. Defaults rather than a backfill: no budget has ever been
written.
"""

import sqlalchemy as sa
from alembic import op

revision = "e4b8a2c61f93"
down_revision = "b7d2e19c4a51"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "budget_line",
        sa.Column(
            "suggested_minor_units", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )
    op.add_column(
        "budget_line",
        sa.Column(
            "is_user_set", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("budget_line", "is_user_set")
    op.drop_column("budget_line", "suggested_minor_units")
