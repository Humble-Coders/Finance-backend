"""A role for the AI coach's SQL that can read nothing, and fuzzy matching

Revision ID: d7b3f9a1c426
Revises: c4e9a2f7b815
Create Date: 2026-10-28

The SQL sandbox (#70) runs a model-written query under `finai_chat_reader`:
NOLOGIN, with no privilege on any table. Inside one transaction the server
copies this household's rows into temporary tables, grants this role SELECT
on those alone, and `SET LOCAL ROLE`s to it — so a query naming a real table
fails with "permission denied", whatever the query says.

The app's database user must be a member to `SET ROLE` to it. Migrations and
the app connect as the same user (`postgres.<project>` on Supabase's pooler,
which is the `postgres` role), so the grant is to CURRENT_USER. If the app
ever connects as a different user, grant that user membership too.

`pg_trgm` gives the sandbox `similarity()`, so "coffee" can be matched to a
household's own merchant names.
"""

from alembic import op

revision = "d7b3f9a1c426"
down_revision = "c4e9a2f7b815"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Roles belong to the cluster, not the database: guarded, so a role left
    # by an earlier run (or another database on the cluster) is reused.
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'finai_chat_reader'
            ) THEN
                CREATE ROLE finai_chat_reader NOLOGIN;
            END IF;
        END
        $$
        """
    )
    op.execute("GRANT finai_chat_reader TO CURRENT_USER")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")


def downgrade() -> None:
    # RESTRICT (the default): refuses if anything else came to depend on it.
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("REVOKE finai_chat_reader FROM CURRENT_USER")
    op.execute("DROP ROLE IF EXISTS finai_chat_reader")
