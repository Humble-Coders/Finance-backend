"""region resolution and signup consent

Revision ID: 5b2e9f7c1d34
Revises: d47f2b91c6ae
Create Date: 2026-09-11 10:00:00.000000
"""
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '5b2e9f7c1d34'
down_revision = 'd47f2b91c6ae'
branch_labels = None
depends_on = None


# Ticket #24 (roadmap 2.1). Three things:
#
#   household_region_change — the audit trail PRD §4.6 requires for region
#     changes, derived or chosen.
#   consent_event — signup consent to a specific terms version (Appendix A.5,
#     item 1).
#   disclaimer_version.kind — so the account terms and a country's regional
#     disclaimer can share the versioned-legal-copy table.
#
# Plus two seed rows. The CA country pack has pointed at "ca-v1" since it was
# seeded, but no row backed it; and consent needs a terms version to point at.
# Both bodies are DRAFT placeholders until counsel reviews the copy (PRD §8 Q13)
# — replaced before launch by adding new versions, never by editing these,
# because consent is logged against the row it was given to.

# Same namespace as the category and country-pack seeds, so a seeded id is the
# same in every environment.
NAMESPACE = uuid.UUID("6f3d9f5e-2a1c-4f52-9d3b-1c7a5e0b4d21")
CA_DISCLAIMER_ID = str(uuid.uuid5(NAMESPACE, "disclaimer_version:CA:ca-v1"))
TERMS_V1_ID = str(uuid.uuid5(NAMESPACE, "disclaimer_version:terms-v1"))

DRAFT_BODY = (
    "DRAFT - placeholder text pending counsel review (PRD section 8, Q13). "
    "Replace with approved copy before launch by adding a new version."
)

region_source = postgresql.ENUM("phone", "user", name="region_source", create_type=False)
policy_kind = postgresql.ENUM(
    "account_terms", "regional_disclaimer", name="policy_kind", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    region_source.create(bind)
    policy_kind.create(bind)

    # Existing rows (none are seeded before this migration) would be regional
    # disclaimers; the default only exists to let the column arrive NOT NULL.
    op.add_column(
        'disclaimer_version',
        sa.Column('kind', policy_kind, nullable=False, server_default='regional_disclaimer'),
    )
    op.alter_column('disclaimer_version', 'kind', server_default=None)

    op.create_table('household_region_change',
    sa.Column('previous_country_code', sa.String(length=2), nullable=True),
    sa.Column('new_country_code', sa.String(length=2), nullable=False),
    sa.Column('source', region_source, nullable=False),
    sa.Column('changed_by_user_id', sa.UUID(), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('household_id', sa.UUID(), nullable=False),
    sa.ForeignKeyConstraint(['changed_by_user_id'], ['user.id'], name=op.f('fk_household_region_change_changed_by_user_id_user'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['household_id'], ['household.id'], name=op.f('fk_household_region_change_household_id_household'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_household_region_change'))
    )
    op.create_index(op.f('ix_household_region_change_household_id'), 'household_region_change', ['household_id'], unique=False)

    op.create_table('consent_event',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('disclaimer_version_id', sa.UUID(), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['disclaimer_version_id'], ['disclaimer_version.id'], name=op.f('fk_consent_event_disclaimer_version_id_disclaimer_version'), ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['user_id'], ['user.id'], name=op.f('fk_consent_event_user_id_user'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_consent_event')),
    # One row per user and terms version, enforced here rather than by a
    # check-then-insert that two simultaneous accepts can both pass. Its index
    # leads with user_id, so it serves per-user lookups too.
    sa.UniqueConstraint('user_id', 'disclaimer_version_id', name=op.f('uq_consent_event_user_id_disclaimer_version_id'))
    )
    op.create_index(op.f('ix_consent_event_disclaimer_version_id'), 'consent_event', ['disclaimer_version_id'], unique=False)

    # Defence in depth, as for every table (7ce291039fe7): RLS on, no policies.
    op.execute('ALTER TABLE "household_region_change" ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE "consent_event" ENABLE ROW LEVEL SECURITY')

    # ON CONFLICT DO NOTHING so a database that already has these rows is fine.
    op.execute(
        f"""
        INSERT INTO disclaimer_version
            (id, country_code, version, kind, body, effective_from, created_at, updated_at)
        VALUES
            ('{CA_DISCLAIMER_ID}', 'CA', 'ca-v1', 'regional_disclaimer', '{DRAFT_BODY}', now(), now(), now()),
            ('{TERMS_V1_ID}', NULL, 'terms-v1', 'account_terms', '{DRAFT_BODY}', now(), now(), now())
        ON CONFLICT DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute('ALTER TABLE "consent_event" DISABLE ROW LEVEL SECURITY')
    op.drop_index(op.f('ix_consent_event_disclaimer_version_id'), table_name='consent_event')
    op.drop_table('consent_event')

    op.execute('ALTER TABLE "household_region_change" DISABLE ROW LEVEL SECURITY')
    op.drop_index(op.f('ix_household_region_change_household_id'), table_name='household_region_change')
    op.drop_table('household_region_change')

    op.execute(
        f"DELETE FROM disclaimer_version WHERE id IN ('{CA_DISCLAIMER_ID}', '{TERMS_V1_ID}')"
    )
    op.drop_column('disclaimer_version', 'kind')

    bind = op.get_bind()
    policy_kind.drop(bind)
    region_source.drop(bind)
