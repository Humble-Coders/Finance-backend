"""monthly expense, and no wizard status

Revision ID: c8d41a6f3b92
Revises: a3f7c2e91b84
Create Date: 2026-09-12 14:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'c8d41a6f3b92'
down_revision = 'a3f7c2e91b84'
branch_labels = None
depends_on = None


# Ticket #29 (roadmap 2.5). Two changes, both from the 2026-09-12 decision that
# the financial setup wizard is part mandatory, part optional (PRD §9):
#
#   + monthly_expense_minor_units — the second mandatory figure. Nullable, and
#     deliberately not backfilled: a household saved before this migration
#     genuinely has no expense figure, and the onboarding rule holds it at the
#     `financial_setup` step until one is supplied. A zero default would be a
#     fabricated answer and would clear the gate for everyone.
#
#   - setup_completed_at / setup_skipped_at — the wizard's status timestamps.
#     The mandatory half is now gated by the onboarding rule, which reads the
#     figures themselves; the optional half needs no status, because a skipped
#     answer and an unasked one are the same fact — no row. Nothing read these.
#
# Money stays integer minor units here and decimal strings on the wire (§4.4).


def upgrade() -> None:
    op.add_column(
        'financial_profile',
        sa.Column('monthly_expense_minor_units', sa.BigInteger(), nullable=True),
    )
    op.drop_column('financial_profile', 'setup_completed_at')
    op.drop_column('financial_profile', 'setup_skipped_at')


def downgrade() -> None:
    op.add_column(
        'financial_profile',
        sa.Column('setup_skipped_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'financial_profile',
        sa.Column('setup_completed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_column('financial_profile', 'monthly_expense_minor_units')
