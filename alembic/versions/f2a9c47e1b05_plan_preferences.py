"""Plan preferences, and the debt_plan feature (#76)

Revision ID: f2a9c47e1b05
Revises: b3e7d1f9a2c4
Create Date: 2026-10-17

One row per household holds the choices behind its plan: the buffer (an
emergency-fund goal), the goal share and the rate line in basis points, and
avalanche or snowball. No row means the person has not chosen yet, and the plan
runs on suggestions.

The `debt_plan` feature is seeded enabled for everyone, as goals' was
(d6a3b8f20e57), so restricting it by plan later is a more specific row.
"""

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "f2a9c47e1b05"
down_revision = "b3e7d1f9a2c4"
branch_labels = None
depends_on = None

NAMESPACE = uuid.UUID("1f6a2c94-7b30-4de6-9a1e-3c5d8b2f0a77")
KEY = "debt_plan"
ROW_ID = str(uuid.uuid5(NAMESPACE, f"feature:{KEY}:None:None"))


def upgrade() -> None:
    strategy = sa.Enum("avalanche", "snowball", name="plan_strategy")
    strategy.create(op.get_bind(), checkfirst=False)
    op.create_table(
        "plan_preference",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("household_id", sa.UUID(), nullable=False),
        sa.Column("buffer_goal_id", sa.UUID(), nullable=True),
        sa.Column("goal_share_bps", sa.Integer(), nullable=False),
        sa.Column("rate_line_bps", sa.Integer(), nullable=False),
        sa.Column(
            "strategy",
            # The dialect's ENUM: `create_type=False` is honoured there, where
            # a plain sa.Enum inside create_table would create the type again.
            postgresql.ENUM(name="plan_strategy", create_type=False),
            nullable=False,
        ),
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
        sa.CheckConstraint(
            "goal_share_bps BETWEEN 0 AND 10000",
            name=op.f("ck_plan_preference_goal_share_is_a_percentage"),
        ),
        sa.CheckConstraint(
            "rate_line_bps BETWEEN 0 AND 10000",
            name=op.f("ck_plan_preference_rate_line_is_a_percentage"),
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_plan_preference_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["buffer_goal_id"],
            ["goal.id"],
            name=op.f("fk_plan_preference_buffer_goal_id_goal"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_preference")),
    )
    op.create_index(
        op.f("ix_plan_preference_household_id"), "plan_preference", ["household_id"]
    )
    op.create_index(
        "uq_plan_preference_household", "plan_preference", ["household_id"], unique=True
    )
    # Row level security on, as on every household-scoped table: this service
    # is the only caller, and RLS stays as defence in depth (CLAUDE.md).
    op.execute("ALTER TABLE plan_preference ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"""
        INSERT INTO feature_availability
            (id, feature_key, country_code, plan, is_enabled, reason,
             created_at, updated_at)
        VALUES ('{ROW_ID}', '{KEY}', NULL, NULL, true, NULL, now(), now())
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(f"DELETE FROM feature_availability WHERE id = '{ROW_ID}'")
    op.drop_index("uq_plan_preference_household", table_name="plan_preference")
    op.drop_index(op.f("ix_plan_preference_household_id"), table_name="plan_preference")
    op.drop_table("plan_preference")
    sa.Enum(name="plan_strategy").drop(op.get_bind(), checkfirst=False)
