"""Your plan's diagnostics

Revision ID: e1c5b7a9d204
Revises: d8b3f6a2c917
Create Date: 2026-10-26

One row per run of a plan diagnostic: the figures the server worked out and
what the AI said about them. The same figures again return the stored run;
a month allows a few fresh ones.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e1c5b7a9d204"
down_revision = "d8b3f6a2c917"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plan_diagnostic",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("household_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("figures", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("advice", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
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
            "char_length(currency) = 3",
            name=op.f("ck_plan_diagnostic_currency_iso4217_length"),
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_plan_diagnostic_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_diagnostic")),
    )
    op.create_index(
        op.f("ix_plan_diagnostic_household_id"), "plan_diagnostic", ["household_id"]
    )
    op.create_index(
        "ix_plan_diagnostic_household_kind_month",
        "plan_diagnostic",
        ["household_id", "kind", "month"],
    )
    # Row level security on, as on every household-scoped table (CLAUDE.md).
    op.execute("ALTER TABLE plan_diagnostic ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index(
        "ix_plan_diagnostic_household_kind_month", table_name="plan_diagnostic"
    )
    op.drop_index(op.f("ix_plan_diagnostic_household_id"), table_name="plan_diagnostic")
    op.drop_table("plan_diagnostic")
