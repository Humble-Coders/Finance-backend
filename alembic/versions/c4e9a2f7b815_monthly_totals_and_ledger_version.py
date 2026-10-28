"""Monthly totals per category, and a ledger version for caches

Revision ID: c4e9a2f7b815
Revises: b3f8d1a6c240
Create Date: 2026-10-28

`monthly_category_total` holds each household's money summed by month,
category and direction, so the AI coach answers a ten-year question from about
a thousand small rows instead of every transaction (#83). It is derived:
`app/services/rollups.py` rebuilds a month's rows whenever the ledger under it
changes, and `python -m app.scripts.rebuild_rollups` fills it for households
that existed before this migration — run it after this one in production.

`ledger_state.ledger_version` goes up with every such rebuild; anything cached
from the ledger is keyed by it.
"""

import sqlalchemy as sa

from alembic import op

revision = "c4e9a2f7b815"
down_revision = "b3f8d1a6c240"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "monthly_category_total",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("household_id", sa.UUID(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("category_id", sa.UUID(), nullable=True),
        sa.Column("category_slug", sa.String(length=64), nullable=True),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("total_minor_units", sa.BigInteger(), nullable=False),
        sa.Column("one_off_minor_units", sa.BigInteger(), nullable=False),
        sa.Column("transaction_count", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "char_length(currency) = 3",
            name=op.f("ck_monthly_category_total_currency_iso4217_length"),
        ),
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_monthly_category_total_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["category_id"],
            ["category.id"],
            name=op.f("fk_monthly_category_total_category_id_category"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monthly_category_total")),
    )
    op.create_index(
        op.f("ix_monthly_category_total_household_id"),
        "monthly_category_total",
        ["household_id"],
    )
    op.create_index(
        "uq_monthly_category_total_key",
        "monthly_category_total",
        [
            "household_id",
            "currency",
            "month",
            sa.text("coalesce(category_id, '00000000-0000-0000-0000-000000000000')"),
            "direction",
        ],
        unique=True,
    )

    op.create_table(
        "ledger_state",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("household_id", sa.UUID(), nullable=False),
        sa.Column(
            "ledger_version",
            sa.BigInteger(),
            server_default=sa.text("0"),
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
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["household.id"],
            name=op.f("fk_ledger_state_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ledger_state")),
    )
    op.create_index(
        op.f("ix_ledger_state_household_id"), "ledger_state", ["household_id"]
    )
    op.create_index(
        "uq_ledger_state_household", "ledger_state", ["household_id"], unique=True
    )

    # Row level security on, as on every household-scoped table (CLAUDE.md).
    op.execute("ALTER TABLE monthly_category_total ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE ledger_state ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_index("uq_ledger_state_household", table_name="ledger_state")
    op.drop_index(op.f("ix_ledger_state_household_id"), table_name="ledger_state")
    op.drop_table("ledger_state")
    op.drop_index("uq_monthly_category_total_key", table_name="monthly_category_total")
    op.drop_index(
        op.f("ix_monthly_category_total_household_id"),
        table_name="monthly_category_total",
    )
    op.drop_table("monthly_category_total")
