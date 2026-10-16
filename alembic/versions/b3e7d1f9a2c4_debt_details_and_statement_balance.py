"""Debt details and a statement's closing balance (#76)

Revision ID: b3e7d1f9a2c4
Revises: e4b7a2c9d13f
Create Date: 2026-10-17

A debt gains what the payoff plan needs beyond a balance and a rate: what it
is, a promotional rate and its end, and when its balance was true and where it
came from. A statement import keeps the balance it closed at, so a card or loan
account linked to a debt can take its balance from the statement.

Existing debts were all typed into the setup wizard: they become `other`, from
`setup`, true as of the day they were entered. Rates are checked as
percentages from here on; the wizard has always refused anything above 100%.
"""

import sqlalchemy as sa

from alembic import op

revision = "b3e7d1f9a2c4"
down_revision = "e4b7a2c9d13f"
branch_labels = None
depends_on = None

KINDS = (
    "credit_card",
    "line_of_credit",
    "car_loan",
    "balance_transfer",
    "student_loan",
    "mortgage",
    "other",
)
SOURCES = ("setup", "manual", "statement")


def upgrade() -> None:
    sa.Enum(*KINDS, name="debt_kind").create(op.get_bind(), checkfirst=False)
    sa.Enum(*SOURCES, name="balance_source").create(op.get_bind(), checkfirst=False)

    op.add_column(
        "debt",
        sa.Column(
            "kind",
            sa.Enum(*KINDS, name="debt_kind", create_type=False),
            nullable=False,
            server_default="other",
        ),
    )
    op.add_column("debt", sa.Column("promo_rate_bps", sa.Integer(), nullable=True))
    op.add_column("debt", sa.Column("promo_ends_on", sa.Date(), nullable=True))
    op.add_column("debt", sa.Column("balance_as_of", sa.Date(), nullable=True))
    op.add_column(
        "debt",
        sa.Column(
            "balance_source",
            sa.Enum(*SOURCES, name="balance_source", create_type=False),
            nullable=False,
            server_default="setup",
        ),
    )
    op.execute("UPDATE debt SET balance_as_of = created_at::date")
    op.create_check_constraint(
        op.f("ck_debt_rate_is_a_percentage"),
        "debt",
        "interest_rate_bps BETWEEN 0 AND 10000",
    )
    op.create_check_constraint(
        op.f("ck_debt_promo_rate_is_a_percentage"),
        "debt",
        "promo_rate_bps BETWEEN 0 AND 10000",
    )
    op.create_check_constraint(
        op.f("ck_debt_promo_rate_and_end_together"),
        "debt",
        "(promo_rate_bps IS NULL) = (promo_ends_on IS NULL)",
    )

    op.add_column(
        "statement_import",
        sa.Column("closing_balance_minor_units", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "statement_import", sa.Column("currency", sa.String(3), nullable=True)
    )
    op.add_column(
        "statement_import", sa.Column("closing_date", sa.Date(), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_statement_import_currency_iso4217_length"),
        "statement_import",
        "char_length(currency) = 3",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_statement_import_currency_iso4217_length"),
        "statement_import",
        type_="check",
    )
    op.drop_column("statement_import", "closing_date")
    op.drop_column("statement_import", "currency")
    op.drop_column("statement_import", "closing_balance_minor_units")

    for name in (
        "ck_debt_promo_rate_and_end_together",
        "ck_debt_promo_rate_is_a_percentage",
        "ck_debt_rate_is_a_percentage",
    ):
        op.drop_constraint(op.f(name), "debt", type_="check")
    op.drop_column("debt", "balance_source")
    op.drop_column("debt", "balance_as_of")
    op.drop_column("debt", "promo_ends_on")
    op.drop_column("debt", "promo_rate_bps")
    op.drop_column("debt", "kind")
    sa.Enum(name="balance_source").drop(op.get_bind(), checkfirst=False)
    sa.Enum(name="debt_kind").drop(op.get_bind(), checkfirst=False)
