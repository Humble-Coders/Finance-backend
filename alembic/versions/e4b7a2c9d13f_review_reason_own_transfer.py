"""own-account transfers: review reason and pair link

Revision ID: e4b7a2c9d13f
Revises: d6a3b8f20e57
Create Date: 2026-10-09

A row filed as a transfer because it looks like money moving between two of
the household's own accounts — a card bill paid from chequing, say — waits
for the person to confirm it (#73). It needs a reason of its own: "we could not
read this" and "this looks like a copy" ask a different question.

`transfer_pair_id` records the other half of such a pair, on both rows, so a
row is paired at most once.

Adding an enum value is one statement. Removing one is not: Postgres has no
DROP VALUE, so downgrade() rebuilds the type, as d91e4b7c2a15 does. Rows
carrying the value are cleared to no reason first — still waiting for review,
which is the truth about them; only the wording of why is lost.
"""

import sqlalchemy as sa

from alembic import op

revision = "e4b7a2c9d13f"
down_revision = "d6a3b8f20e57"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres 12+ allows ADD VALUE inside a transaction; the value just cannot
    # be used before commit, and nothing in this migration uses it.
    op.execute("ALTER TYPE review_reason ADD VALUE IF NOT EXISTS 'own_transfer'")

    op.add_column(
        "transaction", sa.Column("transfer_pair_id", sa.UUID(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_transaction_transfer_pair_id_transaction"),
        "transaction",
        "transaction",
        ["transfer_pair_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_transaction_transfer_pair_id_transaction"),
        "transaction",
        type_="foreignkey",
    )
    op.drop_column("transaction", "transfer_pair_id")

    op.execute(
        "UPDATE transaction SET review_reason = NULL "
        "WHERE review_reason::text = 'own_transfer'"
    )
    op.execute("ALTER TYPE review_reason RENAME TO review_reason_old")
    op.execute(
        "CREATE TYPE review_reason AS ENUM "
        "('low_confidence', 'unknown_category', 'suspected_duplicate')"
    )
    op.execute(
        "ALTER TABLE transaction ALTER COLUMN review_reason "
        "TYPE review_reason USING review_reason::text::review_reason"
    )
    op.execute("DROP TYPE review_reason_old")
