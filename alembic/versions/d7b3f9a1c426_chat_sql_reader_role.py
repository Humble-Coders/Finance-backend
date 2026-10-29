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
household's own merchant names. The reader needs USAGE on the schema it lives
in: locally that is `public`, which everyone may use, but on Supabase it is
`extensions`, which is not granted to PUBLIC — without this grant the function
would not even be found there.
"""

# The schema pg_trgm is installed in, whatever it is.
_TRGM_SCHEMA_GRANT = """
DO $$
DECLARE target text;
BEGIN
    SELECT n.nspname INTO target
    FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
    WHERE e.extname = 'pg_trgm';
    IF target IS NOT NULL THEN
        EXECUTE format('{verb} USAGE ON SCHEMA %I {direction} finai_chat_reader', target);
    END IF;
END
$$
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
    # Explicit options, because the defaults differ by who runs this. In
    # Postgres 16+ a role's creator gets an automatic membership whose SET is
    # off unless `createrole_self_grant` says otherwise — fine for a superuser
    # (CI, local), who may SET ROLE to anything, but not for Supabase's
    # `postgres`, which is not one. SET TRUE is what `SET LOCAL ROLE` needs;
    # INHERIT FALSE keeps the app's user from ever acquiring what the reader
    # is granted (its temporary tables).
    op.execute("GRANT finai_chat_reader TO CURRENT_USER WITH SET TRUE, INHERIT FALSE")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(_TRGM_SCHEMA_GRANT.format(verb="GRANT", direction="TO"))


def downgrade() -> None:
    # Before anything is dropped: a role holding a privilege cannot be.
    op.execute(_TRGM_SCHEMA_GRANT.format(verb="REVOKE", direction="FROM"))
    # RESTRICT (the default): refuses if anything else came to depend on it.
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("REVOKE finai_chat_reader FROM CURRENT_USER")
    op.execute("DROP ROLE IF EXISTS finai_chat_reader")
