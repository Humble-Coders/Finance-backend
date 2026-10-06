"""Goals: a monthly contribution, a kind, and checks on the amounts

Revision ID: c2f8e5a71d34
Revises: a9d3e6b14c72
Create Date: 2026-10-06

M5's goals (backend #65) project forward from what the person plans to put in
each month, so the goal keeps that figure. `kind` picks the picture an app
shows, and nothing more. The checks state what the API already refuses, so
no other writer can store a zero target or a negative balance.

No backfill: the table has never been written to.
"""

import sqlalchemy as sa
from alembic import op

revision = "c2f8e5a71d34"
down_revision = "a9d3e6b14c72"
branch_labels = None
depends_on = None

KINDS = (
    "emergency_fund",
    "vacation",
    "car",
    "electronics",
    "home",
    "retirement",
    "wealth",
    "other",
)
goal_kind = sa.Enum(*KINDS, name="goal_kind")


def upgrade() -> None:
    goal_kind.create(op.get_bind(), checkfirst=False)
    op.add_column(
        "goal",
        sa.Column("monthly_contribution_minor_units", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "goal",
        sa.Column(
            "kind",
            sa.Enum(*KINDS, name="goal_kind", create_type=False),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        op.f("ck_goal_target_positive"), "goal", "target_minor_units > 0"
    )
    op.create_check_constraint(
        op.f("ck_goal_saved_not_negative"), "goal", "saved_minor_units >= 0"
    )
    op.create_check_constraint(
        op.f("ck_goal_monthly_contribution_not_negative"),
        "goal",
        "monthly_contribution_minor_units >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_goal_monthly_contribution_not_negative"), "goal", type_="check"
    )
    op.drop_constraint(op.f("ck_goal_saved_not_negative"), "goal", type_="check")
    op.drop_constraint(op.f("ck_goal_target_positive"), "goal", type_="check")
    op.drop_column("goal", "kind")
    op.drop_column("goal", "monthly_contribution_minor_units")
    goal_kind.drop(op.get_bind(), checkfirst=False)
