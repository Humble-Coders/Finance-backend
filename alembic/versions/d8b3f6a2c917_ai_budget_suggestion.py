"""AI budget suggestions

Revision ID: d8b3f6a2c917
Revises: c4e8a1d7b392
Create Date: 2026-10-25

What the AI suggested for a month's budget — at setup, and once a month after
— stored after the server checked every figure. One row per household, month
and kind, so asking again costs nothing.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d8b3f6a2c917"
down_revision = "c4e8a1d7b392"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_budget_suggestion",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("household_id", sa.UUID(), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("lines", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("savings_minor_units", sa.BigInteger(), nullable=True),
        sa.Column("advice", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
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
            name=op.f("fk_ai_budget_suggestion_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "char_length(currency) = 3",
            name=op.f("ck_ai_budget_suggestion_currency_iso4217_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_budget_suggestion")),
    )
    op.create_index(
        op.f("ix_ai_budget_suggestion_household_id"),
        "ai_budget_suggestion",
        ["household_id"],
    )
    op.create_index(
        "uq_ai_budget_suggestion_household_month_kind",
        "ai_budget_suggestion",
        ["household_id", "month", "kind"],
        unique=True,
    )
    # Row level security on, as on every household-scoped table (CLAUDE.md).
    op.execute("ALTER TABLE ai_budget_suggestion ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index(
        "uq_ai_budget_suggestion_household_month_kind",
        table_name="ai_budget_suggestion",
    )
    op.drop_index(
        op.f("ix_ai_budget_suggestion_household_id"), table_name="ai_budget_suggestion"
    )
    op.drop_table("ai_budget_suggestion")
