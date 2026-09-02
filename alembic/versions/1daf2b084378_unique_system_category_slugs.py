"""unique system category slugs

Revision ID: 1daf2b084378
Revises: ff7d60d4b3b8
Create Date: 2026-09-02 11:54:19.843241
"""
from alembic import op
import sqlalchemy as sa


revision = '1daf2b084378'
down_revision = 'ff7d60d4b3b8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Stop a second *system* category sharing a slug.

    uq_category_household_slug covers (household_id, slug), but Postgres treats
    NULLs as distinct — so it constrains user-defined categories and leaves
    system rows (household_id NULL) unconstrained. A second system "groceries"
    was accepted, and every household would then see two.
    """
    op.create_index(
        "uq_category_system_slug",
        "category",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("household_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_category_system_slug", table_name="category")
