#!/usr/bin/env bash
# Prove every migration applies, reverses and re-applies (ticket #15).
#
# CI runs this against a throwaway Postgres service container. To rehearse
# locally with the same image:
#
#   docker run -d --name finai-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 \
#     ghcr.io/pgmq/pg17-pgmq:v1.5.1
#   DATABASE_URL=postgresql://postgres:postgres@localhost:55432/postgres \
#     PG_CONTAINER=finai-pg PYTHON=.venv/bin/python scripts/check_migrations.sh
#   docker rm -f finai-pg
#
# DESTRUCTIVE: it downgrades to base, which drops every table. A developer's
# .env points at PRODUCTION, so before touching anything it checks, through the
# app's own settings (.env included), that:
#   - both the runtime and the migration DSN name localhost, and neither
#     overrides that with a host= / hostaddr= query parameter;
#   - both reach the SAME server as PG_CONTAINER, compared by Postgres's system
#     identifier. The checks below run inside PG_CONTAINER while Alembic
#     connects through the DSN; without this they could inspect one database
#     while migrating another, and a port-forward to production on localhost
#     would pass a hostname check.
#
# What it proves is migration CORRECTNESS on Postgres 17. It does not prove the
# migrations survive Supabase's transaction pooler; see the database job in
# .github/workflows/ci.yml.
set -euo pipefail

: "${DATABASE_URL:?set DATABASE_URL to the throwaway database}"
: "${PG_CONTAINER:?set PG_CONTAINER to the Postgres container name or id}"

# Alembic uses migration_dsn, which prefers MIGRATION_DATABASE_URL — and a
# developer's .env sets that to the production session pooler. Exporting
# DATABASE_URL alone would still migrate production, so pin both explicitly.
export MIGRATION_DATABASE_URL="$DATABASE_URL"

PYTHON="${PYTHON:-python}"
ALEMBIC=("$PYTHON" -m alembic)
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

step() { printf '\n==> %s\n' "$*"; }
fail() {
  printf '\nFAIL: %s\n' "$*" >&2
  exit 1
}

step "Refusing anything but the throwaway database in PG_CONTAINER"
# Resolved through Settings rather than by parsing the variable: the question
# is which database the app and Alembic will actually connect to. And it
# connects, rather than trusting the hostname — see the header.
target="$("$PYTHON" - <<'PY'
import asyncio
import sys

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings

LOCAL = {"localhost", "127.0.0.1", "::1"}


async def system_identifier(dsn: str) -> int:
    # The same URL-to-connection path Alembic takes, so a query-string host is
    # followed here exactly as it would be there.
    engine = create_async_engine(
        dsn,
        poolclass=NullPool,
        connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0},
    )
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT system_identifier FROM pg_control_system()"))
            return result.scalar_one()
    finally:
        await engine.dispose()


settings = get_settings()
identifiers = set()
for label, dsn in (("runtime", settings.database_dsn), ("migration", settings.migration_dsn)):
    url = make_url(dsn)
    overrides = sorted({"host", "hostaddr"} & set(url.query))
    if overrides:
        sys.exit(
            f"{label} DSN sets {', '.join(overrides)} in its query string, which overrides "
            "the host it names — refusing"
        )
    if url.host not in LOCAL:
        sys.exit(f"{label} DSN points at {url.host!r}, not localhost — refusing to drop its tables")
    try:
        identifiers.add(asyncio.run(system_identifier(dsn)))
    except Exception as exc:
        sys.exit(f"cannot query the server behind the {label} DSN: {exc}")
if len(identifiers) != 1:
    sys.exit("the runtime and migration DSNs reach different servers — refusing")
url = make_url(settings.migration_dsn)
print(url.username, url.database, identifiers.pop())
PY
)"
read -r DB_USER DB_NAME DSN_SERVER <<<"$target"

# No -i: psql -c never reads stdin, and an attached stdin would swallow
# whatever the caller is feeding this script — a heredoc-driven run once lost
# the rest of its commands that way.
psql_c() {
  docker exec "$PG_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -tAX -v ON_ERROR_STOP=1 -c "$1"
}

# Captured by assignment, never tested inline: a failing $(...) inside [ ]
# does not trip set -e, so an unreachable container would read as a match or
# as "empty". An assignment fails closed.
container_server="$(psql_c "SELECT system_identifier FROM pg_control_system()")" ||
  fail "cannot query PG_CONTAINER ($PG_CONTAINER) — is it running?"
[ "$container_server" = "$DSN_SERVER" ] ||
  fail "the DSN reaches server $DSN_SERVER but PG_CONTAINER ($PG_CONTAINER) is server $container_server — the checks would inspect one database while Alembic migrates another"
echo "ok: localhost, no host override, and the DSN reaches the same server as $PG_CONTAINER ($DSN_SERVER)"

# pg_dump runs inside the container, so the runner's own client version cannot
# disagree with the server. Patched pg_dump (17.6+) brackets its output with
# \restrict / \unrestrict lines carrying a random key — strip them, or two
# dumps of an identical schema would never compare equal.
dump_schema() {
  docker exec "$PG_CONTAINER" pg_dump --schema-only --no-owner --no-privileges \
    -U "$DB_USER" -d "$DB_NAME" | sed -e '/^\\restrict /d' -e '/^\\unrestrict /d'
}

current() { "${ALEMBIC[@]}" current 2>/dev/null; }

queue_count() {
  psql_c "SELECT count(*) FROM pgmq.list_queues() WHERE queue_name = 'extraction_jobs'"
}

# Everything a downgrade could leave behind in the application schema. The
# enum types are here on purpose: an autogenerated downgrade() once dropped the
# tables but left seven enum types in place (ticket #10).
LEFTOVERS_SQL="
SELECT kind || ' ' || name FROM (
  SELECT 'table' AS kind, tablename AS name FROM pg_tables
    WHERE schemaname = 'public' AND tablename <> 'alembic_version'
  UNION ALL SELECT 'enum', t.typname FROM pg_type t
    JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname = 'public' AND t.typtype = 'e'
  UNION ALL SELECT 'sequence', sequencename FROM pg_sequences WHERE schemaname = 'public'
  UNION ALL SELECT 'view', viewname FROM pg_views WHERE schemaname = 'public'
  UNION ALL SELECT 'function', p.proname FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public'
) leftovers ORDER BY 1"

step "Starting from an empty database"
initial="$(psql_c "$LEFTOVERS_SQL")" || fail "cannot inspect the database in $PG_CONTAINER"
[ -z "$initial" ] || fail "the database is not empty — start from a fresh container:
$initial"

step "Enabling pgmq, as production has it"
# The image makes pgmq available; this makes it installed. The queue migration
# checks pg_extension, so without this its real path would be skipped here.
psql_c "CREATE EXTENSION IF NOT EXISTS pgmq" >/dev/null ||
  fail "could not enable pgmq (psql's error is above) — without it the queue migration's real path goes untested"

step "Exactly one migration head"
heads="$("${ALEMBIC[@]}" heads 2>&1)"
count="$(grep -c '(head)' <<<"$heads" || true)"
[ "$count" -eq 1 ] || fail "expected one head, found $count — two migrations share a parent, which is a merge-order bug:
$heads"
echo "ok: $heads"

step "upgrade head — every migration applies to an empty database"
"${ALEMBIC[@]}" upgrade head
first="$(current)"
dump_schema >"$WORK/first.sql"
[ "$(queue_count)" = "1" ] || fail "no extraction_jobs queue after upgrade — the guarded create did not run"
echo "ok: at $first, queue created"

step "upgrade head again — a no-op the second time"
"${ALEMBIC[@]}" upgrade head
[ "$(current)" = "$first" ] || fail "the second upgrade moved the revision: $(current)"
dump_schema >"$WORK/again.sql"
diff -u "$WORK/first.sql" "$WORK/again.sql" || fail "the second upgrade changed the schema"
echo "ok: revision and schema unchanged"

step "downgrade base — every downgrade() runs, and leaves nothing behind"
"${ALEMBIC[@]}" downgrade base
leftovers="$(psql_c "$LEFTOVERS_SQL")"
[ -z "$leftovers" ] || fail "downgrade base left objects behind:
$leftovers"
# Kept deliberately: dropping the queue would destroy in-flight uploads.
[ "$(queue_count)" = "1" ] || fail "the queue did not survive downgrade — its downgrade() is meant to be a no-op"
echo "ok: schema empty, queue kept by design"

step "upgrade head once more — the round trip reproduces the schema exactly"
# This is also where the queue migration meets an existing queue, which is the
# guard production depends on.
"${ALEMBIC[@]}" upgrade head
dump_schema >"$WORK/second.sql"
diff -u "$WORK/first.sql" "$WORK/second.sql" ||
  fail "the round trip changed the schema — some downgrade() does not exactly undo its upgrade()"
[ "$(queue_count)" = "1" ] || fail "the queue guard created a duplicate or failed on re-apply"
echo "ok: identical schema, one queue"

printf '\nAll migration checks passed in %ss.\n' "$SECONDS"
