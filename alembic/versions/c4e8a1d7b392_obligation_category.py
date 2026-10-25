"""A commitment's budget category

Revision ID: c4e8a1d7b392
Revises: f2a9c47e1b05
Create Date: 2026-10-23

Each obligation can name the spending category it belongs to, so the budget
never allocates a line less than the commitments in it (Rent $1,800 holds the
`rent` line at $1,800 or more). Nullable: rows saved before this keep working,
and fall back on the preset names in `app/services/commitments.py`.
"""

import sqlalchemy as sa

from alembic import op

revision = "c4e8a1d7b392"
down_revision = "f2a9c47e1b05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("obligation", sa.Column("category_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("fk_obligation_category_id_category"),
        "obligation",
        "category",
        ["category_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_obligation_category_id_category"), "obligation", type_="foreignkey"
    )
    op.drop_column("obligation", "category_id")
