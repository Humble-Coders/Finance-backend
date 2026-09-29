"""consent_change: withdrawing consent, and giving it again (#42)

Revision ID: a238d28cd057
Revises: a3f91c2e77b4
Create Date: 2026-09-29

PIPEDA gives the right to withdraw consent at any time; the product had no way
to. Withdrawal is recorded as an event beside `consent_event` rather than by
changing it:

* `consent_event` is the proof of which text someone agreed to, and deleting
  or editing it would erase the evidence that consent existed when data was
  processed.
* Its one-row-per-(user, version) constraint is what makes a double-tapped
  "I agree" harmless — and it means re-consent to the same version cannot add a
  second row there. The sequence has to live somewhere else.

**Purely additive, on purpose.** A new enum type and a new table; nothing that
exists changes. Production has one database and it is migrated by hand, so the
code and the schema arrive minutes apart in some order — this way either order
works. (Relaxing `consent_event`'s constraint instead would have broken every
consent write, account terms included, until the deploy caught up.)

Postgres allows a new enum type to be used in the same transaction that
created it when it is only a column type; there is no data insert here.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a238d28cd057"
down_revision = "a3f91c2e77b4"
branch_labels = None
depends_on = None

consent_action = postgresql.ENUM(
    "given", "withdrawn", name="consent_action", create_type=False
)
policy_kind = postgresql.ENUM(name="policy_kind", create_type=False)


def upgrade() -> None:
    consent_action.create(op.get_bind())
    op.create_table(
        "consent_change",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("kind", policy_kind, nullable=False),
        sa.Column("action", consent_action, nullable=False),
        sa.Column("disclaimer_version_id", sa.UUID(), nullable=True),
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_consent_change_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["disclaimer_version_id"],
            ["disclaimer_version.id"],
            name=op.f("fk_consent_change_disclaimer_version_id_disclaimer_version"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consent_change")),
    )
    op.create_index(
        "ix_consent_change_user_kind_created",
        "consent_change",
        ["user_id", "kind", "created_at"],
        unique=False,
    )
    # Defence in depth, as for every table (7ce291039fe7): RLS on, no policies.
    op.execute('ALTER TABLE "consent_change" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.execute('ALTER TABLE "consent_change" DISABLE ROW LEVEL SECURITY')
    op.drop_index("ix_consent_change_user_kind_created", table_name="consent_change")
    op.drop_table("consent_change")
    consent_action.drop(op.get_bind())
