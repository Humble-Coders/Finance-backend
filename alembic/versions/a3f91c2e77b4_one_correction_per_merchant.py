"""one category correction per merchant, per household

Revision ID: a3f91c2e77b4
Revises: c7e1a93b4d82
Create Date: 2026-09-28

A correction is a rule, not a diary entry. Correcting SPOTIFY to Entertainment
twice should leave one rule saying Entertainment — not two rows that a later
reader has to pick between, and not a prompt fed both answers at once.

The application upserts on this key, but a unique index is what makes the rule
true: two review screens open on the same merchant, or a retried request, both
reach this table concurrently and the application check alone would let the
second through.

`merchant_pattern` already carried a plain index for lookups; this replaces it,
because a unique index serves those reads just as well and leaving both would
keep two structures over one column for no gain.
"""

from alembic import op

revision = "a3f91c2e77b4"
down_revision = "c7e1a93b4d82"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The plain lookup index is subsumed by the unique one below: Postgres uses
    # a unique index for equality reads exactly as it would a plain one.
    op.drop_index("ix_category_correction_merchant_pattern", table_name="category_correction")
    op.create_index(
        "uq_category_correction_household_merchant",
        "category_correction",
        ["household_id", "merchant_pattern"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_category_correction_household_merchant", table_name="category_correction"
    )
    op.create_index(
        "ix_category_correction_merchant_pattern",
        "category_correction",
        ["merchant_pattern"],
    )
