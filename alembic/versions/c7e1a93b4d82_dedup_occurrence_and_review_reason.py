"""dedup occurrence, review reason, account name uniqueness

Revision ID: c7e1a93b4d82
Revises: b2d5f8a13c47
Create Date: 2026-09-22

Three additions, all in service of one problem: telling a real repeat purchase
apart from a re-imported one.

Two $5.25 coffees at the same shop on the same day produce byte-identical rows,
and so does importing one coffee twice. `occurrence` numbers them, so the
database can reject the second import exactly while still accepting a genuine
third coffee.

`review_reason` and `duplicate_of_id` are what make a flagged row actionable in
3.4: which question is being asked, and what the row collided with.
"""

import sqlalchemy as sa
from alembic import op

revision = "c7e1a93b4d82"
down_revision = "b2d5f8a13c47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The default stays. "The first of its group" is the right answer for any
    # row that does not think about this — a manually typed transaction, a
    # future aggregator feed, a test inserting a row to check something else.
    # Dropping it would make every other insert path name a column it has no
    # opinion about, and the import path sets it explicitly regardless.
    op.add_column(
        "transaction",
        sa.Column("occurrence", sa.Integer(), nullable=False, server_default="1"),
    )

    op.drop_index("uq_transaction_dedup", table_name="transaction")
    op.create_index(
        "uq_transaction_dedup",
        "transaction",
        [
            "account_id",
            "occurred_on",
            "amount_minor_units",
            "normalized_description",
            "occurrence",
        ],
        unique=True,
    )

    review_reason = sa.Enum(
        "low_confidence",
        "unknown_category",
        "suspected_duplicate",
        name="review_reason",
    )
    review_reason.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "transaction", sa.Column("review_reason", review_reason, nullable=True)
    )
    op.add_column(
        "transaction", sa.Column("duplicate_of_id", sa.UUID(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_transaction_duplicate_of_id_transaction"),
        "transaction",
        "transaction",
        ["duplicate_of_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_index(
        "uq_account_household_name", "account", ["household_id", "name"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_account_household_name", table_name="account")
    op.drop_constraint(
        op.f("fk_transaction_duplicate_of_id_transaction"),
        "transaction",
        type_="foreignkey",
    )
    op.drop_column("transaction", "duplicate_of_id")
    op.drop_column("transaction", "review_reason")
    sa.Enum(name="review_reason").drop(op.get_bind(), checkfirst=True)

    op.drop_index("uq_transaction_dedup", table_name="transaction")
    op.create_index(
        "uq_transaction_dedup",
        "transaction",
        ["account_id", "occurred_on", "amount_minor_units", "normalized_description"],
        unique=True,
    )
    op.drop_column("transaction", "occurrence")
