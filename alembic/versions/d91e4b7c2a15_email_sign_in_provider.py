"""email sign-in provider

Revision ID: d91e4b7c2a15
Revises: c8d41a6f3b92
Create Date: 2026-09-15 15:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'd91e4b7c2a15'
down_revision = 'c8d41a6f3b92'
branch_labels = None
depends_on = None


# Email + password becomes a sign-in route of its own (manager decision,
# 2026-09-15 — PRD §9), so user_identity needs a provider value for it.
#
# Adding an enum value is one statement. Removing one is not: Postgres has no
# DROP VALUE, so downgrade() rebuilds the type — set the old one aside, create
# it again without 'email', re-point the column, drop the old.
#
# It refuses first if any identity still uses 'email'. Casting such a row to
# the narrower type would fail halfway through the rebuild, and there is no
# value it could honestly be rewritten to: an email identity is not a phone,
# Google or Apple one.


def upgrade() -> None:
    # Postgres 12+ allows ADD VALUE inside a transaction; the value just cannot
    # be used before commit, and nothing in this migration uses it.
    op.execute("ALTER TYPE auth_provider ADD VALUE IF NOT EXISTS 'email'")


def downgrade() -> None:
    in_use = op.get_bind().execute(
        sa.text("SELECT count(*) FROM user_identity WHERE provider::text = 'email'")
    ).scalar()
    if in_use:
        raise RuntimeError(
            f"{in_use} user_identity row(s) still use provider 'email'. Remove "
            "them before downgrading: Postgres cannot drop an enum value, and "
            "there is no equivalent provider to rewrite them to."
        )
    op.execute("ALTER TYPE auth_provider RENAME TO auth_provider_old")
    op.execute("CREATE TYPE auth_provider AS ENUM ('phone', 'google', 'apple')")
    op.execute(
        "ALTER TABLE user_identity ALTER COLUMN provider "
        "TYPE auth_provider USING provider::text::auth_provider"
    )
    op.execute("DROP TYPE auth_provider_old")
