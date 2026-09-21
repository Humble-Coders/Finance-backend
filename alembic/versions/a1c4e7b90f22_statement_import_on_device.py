"""statement import: no document, on-device extraction

Revision ID: a1c4e7b90f22
Revises: d91e4b7c2a15
Create Date: 2026-09-21

The 2026-09-21 decision (PRD §9) moved extraction onto the device: the statement
is read and redacted there, and only text is sent. `document_upload` described a
file that no longer reaches us, so this renames it and drops the four columns
that only made sense for a stored file.

Renaming rather than dropping and recreating, deliberately — the table is empty
today, but a rename keeps the ids, the RLS flag and the foreign key from
`transaction` intact, and it is the only form of this change that stays correct
if a row ever does exist when it runs.
"""

import sqlalchemy as sa
from alembic import op

revision = "a1c4e7b90f22"
down_revision = "d91e4b7c2a15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE document_status RENAME TO statement_import_status")
    op.rename_table("document_upload", "statement_import")

    # Postgres carries index and constraint names through a table rename, so
    # they keep the old table's name until renamed explicitly. Left alone, a
    # later autogenerate would see a mismatch and try to "fix" it.
    op.execute(
        "ALTER INDEX ix_document_upload_household_id "
        "RENAME TO ix_statement_import_household_id"
    )
    op.execute(
        "ALTER INDEX ix_document_upload_status RENAME TO ix_statement_import_status"
    )
    op.execute("ALTER INDEX pk_document_upload RENAME TO pk_statement_import")
    op.execute(
        "ALTER TABLE statement_import RENAME CONSTRAINT "
        "fk_document_upload_household_id_household TO "
        "fk_statement_import_household_id_household"
    )

    # The file that is never received.
    op.drop_column("statement_import", "storage_path")
    op.drop_column("statement_import", "content_type")
    op.drop_column("statement_import", "byte_size")
    op.drop_column("statement_import", "deleted_at")
    # `original_filename` goes too: it is personal data ("statement-jane.pdf")
    # that the device has no reason to send and we have no use for.
    op.drop_column("statement_import", "original_filename")

    source_kind = sa.Enum("pdf_text", "ocr", name="source_kind")
    source_kind.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "statement_import",
        # Nullable-then-backfill-then-NOT NULL is the usual dance; the table is
        # empty, so a server_default is enough and is dropped immediately after
        # so the application must always say which it was.
        sa.Column(
            "source_kind", source_kind, nullable=False, server_default="pdf_text"
        ),
    )
    op.alter_column("statement_import", "source_kind", server_default=None)
    op.add_column(
        "statement_import", sa.Column("page_count", sa.Integer(), nullable=True)
    )

    op.alter_column(
        "transaction", "document_upload_id", new_column_name="statement_import_id"
    )
    op.execute(
        "ALTER TABLE transaction RENAME CONSTRAINT "
        "fk_transaction_document_upload_id_document_upload TO "
        "fk_transaction_statement_import_id_statement_import"
    )

    op.create_table(
        "statement_import_text",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("household_id", sa.UUID(), nullable=False),
        sa.Column("statement_import_id", sa.UUID(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
            ["household_id"],
            ["household.id"],
            name=op.f("fk_statement_import_text_household_id_household"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["statement_import_id"],
            ["statement_import.id"],
            name=op.f("fk_statement_import_text_statement_import_id_statement_import"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_statement_import_text")),
        sa.UniqueConstraint(
            "statement_import_id", name=op.f("uq_statement_import_text_import")
        ),
    )
    op.create_index(
        op.f("ix_statement_import_text_household_id"),
        "statement_import_text",
        ["household_id"],
    )
    op.create_index(
        op.f("ix_statement_import_text_expires_at"),
        "statement_import_text",
        ["expires_at"],
    )
    # Same posture as every other table: RLS on, no policies, so any connection
    # that does not bypass RLS sees nothing (migration 7ce291039fe7).
    op.execute("ALTER TABLE statement_import_text ENABLE ROW LEVEL SECURITY")

    # Consent to AI processing, asked before the first import and recorded
    # against its own policy version (PRD Appendix A.5 #1).
    op.execute("ALTER TYPE policy_kind ADD VALUE IF NOT EXISTS 'ai_processing'")


def downgrade() -> None:
    op.drop_table("statement_import_text")

    op.execute(
        "ALTER TABLE transaction RENAME CONSTRAINT "
        "fk_transaction_statement_import_id_statement_import TO "
        "fk_transaction_document_upload_id_document_upload"
    )
    op.alter_column(
        "transaction", "statement_import_id", new_column_name="document_upload_id"
    )

    op.drop_column("statement_import", "page_count")
    op.drop_column("statement_import", "source_kind")
    sa.Enum(name="source_kind").drop(op.get_bind(), checkfirst=True)

    op.add_column(
        "statement_import",
        sa.Column("original_filename", sa.String(512), nullable=True),
    )
    op.add_column(
        "statement_import",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "statement_import", sa.Column("byte_size", sa.Integer(), nullable=True)
    )
    op.add_column(
        "statement_import", sa.Column("content_type", sa.String(128), nullable=True)
    )
    op.add_column(
        "statement_import", sa.Column("storage_path", sa.String(1024), nullable=True)
    )

    op.execute(
        "ALTER TABLE statement_import RENAME CONSTRAINT "
        "fk_statement_import_household_id_household TO "
        "fk_document_upload_household_id_household"
    )
    op.execute("ALTER INDEX pk_statement_import RENAME TO pk_document_upload")
    op.execute(
        "ALTER INDEX ix_statement_import_status RENAME TO ix_document_upload_status"
    )
    op.execute(
        "ALTER INDEX ix_statement_import_household_id "
        "RENAME TO ix_document_upload_household_id"
    )
    op.rename_table("statement_import", "document_upload")
    op.execute("ALTER TYPE statement_import_status RENAME TO document_status")
    # `policy_kind`'s new value stays: Postgres cannot drop an enum member, and
    # rebuilding the type on the way down would be more dangerous than the value
    # being unused.
