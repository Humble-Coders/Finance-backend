"""Your plan's choices as priorities and percentages, and the month's split

Revision ID: b8d2f4a6c913
Revises: a7c4e2f9b318
Create Date: 2026-10-31

The redesign (2026-10-31): the person orders the four places the money left
over can go — their Emergency fund goal, extra debt payment, their other goals,
save or invest — and gives each a percentage. `plan_preference.weights` keeps
them; null until saved, when the plan runs on suggested ones.

`plan_split` keeps a month's "Where it goes" as last worked out — by the AI,
checked by the server, or by the rules when the AI is not asked — with the
person's own edits, so the AI is asked again only when what the split depends
on changes.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b8d2f4a6c913"
down_revision = "a7c4e2f9b318"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plan_split",
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("figures", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("why", sa.String(length=600), nullable=True),
        sa.Column("edited", sa.Boolean(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("ai_runs", sa.Integer(), nullable=False),
        sa.Column("id", sa.UUID(), nullable=False),
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
        sa.Column("household_id", sa.UUID(), nullable=False),
        sa.CheckConstraint(
            "char_length(currency) = 3",
            name=op.f("ck_plan_split_currency_iso4217_length"),
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_plan_split_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plan_split")),
    )
    op.create_index(
        op.f("ix_plan_split_household_id"), "plan_split", ["household_id"], unique=False
    )
    op.create_index(
        "uq_plan_split_household_month",
        "plan_split",
        ["household_id", "month"],
        unique=True,
    )
    op.execute("ALTER TABLE plan_split ENABLE ROW LEVEL SECURITY")
    op.add_column(
        "plan_preference",
        sa.Column("weights", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("plan_preference", "weights")
    op.drop_index("uq_plan_split_household_month", table_name="plan_split")
    op.drop_index(op.f("ix_plan_split_household_id"), table_name="plan_split")
    op.drop_table("plan_split")
