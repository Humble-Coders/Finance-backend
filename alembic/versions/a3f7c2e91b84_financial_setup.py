"""financial setup wizard

Revision ID: a3f7c2e91b84
Revises: 5b2e9f7c1d34
Create Date: 2026-09-12 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = 'a3f7c2e91b84'
down_revision = '5b2e9f7c1d34'
branch_labels = None
depends_on = None


# Ticket #25 (roadmap 2.2). Somewhere for the setup wizard to save (PRD F1):
#
#   financial_profile — one row per household: income, the currency those
#     amounts are in, and the two timestamps that status is derived from.
#   obligation / investment — the repeating lists; wholly wizard-owned, so a
#     save replaces them outright.
#   debt.entered_via_setup — debts are shared with M3's statement import, so
#     the wizard may only replace its own rows.
#
# Money is integer minor units here and decimal strings on the wire (PRD §4.4).


def upgrade() -> None:
    op.create_table('financial_profile',
    sa.Column('monthly_income_minor_units', sa.BigInteger(), nullable=True),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('setup_completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('setup_skipped_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('household_id', sa.UUID(), nullable=False),
    sa.CheckConstraint('char_length(currency) = 3', name=op.f('ck_financial_profile_currency_iso4217_length')),
    sa.ForeignKeyConstraint(['household_id'], ['household.id'], name=op.f('fk_financial_profile_household_id_household'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_financial_profile')),
    sa.UniqueConstraint('household_id', name=op.f('uq_financial_profile_household_id'))
    )
    op.create_index(op.f('ix_financial_profile_household_id'), 'financial_profile', ['household_id'], unique=False)

    op.create_table('obligation',
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('monthly_amount_minor_units', sa.BigInteger(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('household_id', sa.UUID(), nullable=False),
    sa.CheckConstraint('char_length(currency) = 3', name=op.f('ck_obligation_currency_iso4217_length')),
    sa.ForeignKeyConstraint(['household_id'], ['household.id'], name=op.f('fk_obligation_household_id_household'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_obligation'))
    )
    op.create_index(op.f('ix_obligation_household_id'), 'obligation', ['household_id'], unique=False)

    op.create_table('investment',
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('amount_minor_units', sa.BigInteger(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('household_id', sa.UUID(), nullable=False),
    sa.CheckConstraint('char_length(currency) = 3', name=op.f('ck_investment_currency_iso4217_length')),
    sa.ForeignKeyConstraint(['household_id'], ['household.id'], name=op.f('fk_investment_household_id_household'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_investment'))
    )
    op.create_index(op.f('ix_investment_household_id'), 'investment', ['household_id'], unique=False)

    # Existing debts predate the wizard, so they are not its rows: false.
    # The default is temporary — the model carries no server default.
    op.add_column('debt', sa.Column('entered_via_setup', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.alter_column('debt', 'entered_via_setup', server_default=None)

    # Defence in depth, as for every table (7ce291039fe7): RLS on, no policies.
    for table in ('financial_profile', 'obligation', 'investment'):
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    for table in ('investment', 'obligation', 'financial_profile'):
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')

    op.drop_column('debt', 'entered_via_setup')

    op.drop_index(op.f('ix_investment_household_id'), table_name='investment')
    op.drop_table('investment')
    op.drop_index(op.f('ix_obligation_household_id'), table_name='obligation')
    op.drop_table('obligation')
    op.drop_index(op.f('ix_financial_profile_household_id'), table_name='financial_profile')
    op.drop_table('financial_profile')
