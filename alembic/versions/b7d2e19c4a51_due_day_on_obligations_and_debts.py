"""Due day on obligations and debts

Revision ID: b7d2e19c4a51
Revises: 85ad4e1e92a0
Create Date: 2026-10-05

The day of the month an obligation or a debt payment falls due, so the apps
can say "Due 5 Oct" and list what is coming up. Nullable with no default: a
due date nobody gave is not a fact, and a default would print one.
"""

import sqlalchemy as sa
from alembic import op

revision = "b7d2e19c4a51"
down_revision = "85ad4e1e92a0"
branch_labels = None
depends_on = None

TABLES = ("obligation", "debt")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(table, sa.Column("due_day", sa.SmallInteger(), nullable=True))
        op.create_check_constraint(
            op.f(f"ck_{table}_due_day_in_month"),
            table,
            "due_day BETWEEN 1 AND 31",
        )


def downgrade() -> None:
    for table in TABLES:
        op.drop_constraint(op.f(f"ck_{table}_due_day_in_month"), table, type_="check")
        op.drop_column(table, "due_day")
