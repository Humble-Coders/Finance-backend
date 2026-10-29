# Handoff — ticket #70

**Ticket:** #70 — [M6] A household-isolated SQL sandbox for the chatbot — the fallback

## Summary

The AI coach can now run **one SELECT it wrote itself**, for questions no fixed tool answers. The query runs so that one household can never see another's data, whatever SQL the model writes. Four walls, each of which stops a leak on its own:

1. **A validator** built on PostgreSQL's own parser (`pglast` 7.21). It allows one SELECT, only sandbox table names and in-scope CTEs, and only allowlisted functions, node types and casts. What runs is the deparsed, validated statement.
2. **Temporary tables** hold only this household's rows, and only for the tables the query names. Amounts become exact decimals.
3. **`SET LOCAL ROLE finai_chat_reader`**, a new role with SELECT on those temporary tables only.
4. **Read-only and bounded:** `SET TRANSACTION READ ONLY`, a 2-second `statement_timeout`, and at most 50 rows with the true count.

Everything runs as one prepared statement inside a savepoint that is always rolled back. A `run_sql` tool spec, a handler and a schema paragraph are ready for 6.4 to register. 60 tests attack the sandbox, including each wall on its own.

## Files changed

**Schema**
- `alembic/versions/d7b3f9a1c426_chat_sql_reader_role.py`:
  - creates the `finai_chat_reader` role (NOLOGIN), guarded because roles are cluster-wide;
  - `GRANT finai_chat_reader TO CURRENT_USER WITH SET TRUE, INHERIT FALSE`, so the app's user can switch to it but never inherits what it is granted;
  - `CREATE EXTENSION IF NOT EXISTS pg_trgm`, for `similarity()`.

  The downgrade drops the extension (RESTRICT), revokes the grant and drops the role.
- `requirements.txt`: pins `pglast==7.21`.

**Money**
- `app/core/money.py`: `sql_major_units(column, currency)` is a SQL expression that turns minor units into an exact `numeric` amount scaled to the currency. The sandbox's money columns are already in dollars, and the conversion stays in `money.py`.

**The sandbox:** `app/services/chat_sql.py`
- **`validate(query) -> Validated(tables, sql)`:**
  - parses the query and walks the syntax tree;
  - only a fixed set of node types is allowed, so anything unforeseen is closed by default;
  - CTEs are scoped as Postgres resolves them;
  - function, cast and `CURRENT_*` value allowlists;
  - no schema qualification on tables, functions, casts or operators, except `pg_catalog.` (for example `EXTRACT`, which parses as `pg_catalog.extract`);
  - no `INTO`, locking, parameters or `WITH RECURSIVE`.
- **`SANDBOX_TABLES`:** `monthly_totals`, `transactions`, `budget_lines`, `goals`, `debts`, `obligations` and `merchants`.
  - Each has fixed columns and a source query filtered by household and currency.
  - `transactions` holds `countable` rows only.
  - `monthly_totals` and `transactions` carry `is_flow`, so spending matches `/dashboard`.
- **`run(conn, household_id, currency, query)`:**
  1. Opens a savepoint.
  2. Copies the named tables into temporary tables (`ON COMMIT DROP`).
  3. Grants SELECT on them to the reader role.
  4. Runs `SET TRANSACTION READ ONLY`, `SET LOCAL ROLE` and `SET LOCAL statement_timeout = '2s'`.
  5. Runs one statement, `SELECT *, count(*) OVER () AS _total FROM (<sql>) q LIMIT 50`.
  6. Rolls back the savepoint, always.

  Results: decimal strings, ISO dates, never a float.
- **Errors:** every failure goes back to the model as a `ToolError`, whether a validator rejection, a database error, a timeout or a driver failure.
- **Audit log:** one `chat_sql` line per query, with the household, tables, rows copied, rows returned, duration, outcome and a 16-character SHA-256 hash of the query.
- **For 6.4:** `RUN_SQL`, the tool spec; `handler_for(household_id, currency)`, which runs on a connection of its own and rolls back; `SCHEMA_PROMPT`, the paragraph for the cached system prompt.

**Tests**
- `tests/test_chat_sql.py`: 23 test functions, which expand to 60 test cases (the validator test is parametrized over 38 attacks). All are `integration`.

## How to test

These steps use the local Docker database only, never `.env`.

1. Migrate the local container:
   ```bash
   cd ~/AndroidStudioProjects/Finance-backend && DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres MIGRATION_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres .venv/bin/python -m alembic upgrade head
   ```
2. Run the sandbox tests. Expect 60 passed:
   ```bash
   DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres MIGRATION_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:55432/postgres .venv/bin/pytest -q tests/test_chat_sql.py
   ```
3. Run both suites. Expect 525 without the database and 793 with it (733 before this ticket).
4. Run the migration round trip. It needs an **empty** throwaway container on port 55432:
   ```bash
   DATABASE_URL=postgresql://postgres:postgres@localhost:55432/postgres PG_CONTAINER=<container> PYTHON=.venv/bin/python scripts/check_migrations.sh
   ```
   It passed locally.
5. Lint:
   ```bash
   .venv/bin/ruff check app tests && .venv/bin/ruff format --check app tests
   ```

## Acceptance criteria

| Criterion | Status | Where |
|---|---|---|
| **Isolation, attacked:** as household A, the attacks return nothing of B's (`SELECT * FROM transaction`, `set_config`, a `UNION` with a real table, a CTE naming a real table, schema-qualified names, `pg_catalog` reads, comment and quoting tricks) | Met | The `REJECTED` table (38 attacks) runs through the validator and through the whole sandbox (`test_the_attacks_return_nothing_of_anothers`) |
| Each defence holds on its own | Met | Validator bypassed: real tables are "permission denied", including after an in-statement `set_config('role')`; `pg_stats` is refused; sandbox names hold only A's rows. Role skipped: the validator still refuses. Validator and role both off: the temp tables alone hold only A |
| Writes impossible: `INSERT`, `UPDATE`, `DELETE`, DDL and `SELECT … INTO` rejected, and if forced past the validator they fail under `READ ONLY` | Met | All rejected by the validator. Forced past it, DML can't even be wrapped, and `lo_create` fails with "read-only transaction" even with the role skipped |
| Only the tables a query names are copied (from the audit line) | Met | `test_a_monthly_totals_query_copies_no_transactions` |
| Ten years: a yearly total over `monthly_totals` and a merchant total over `transactions` finish inside 2 s and equal the raw sums; a 10-year listing returns 50 rows, the true `_total` and `truncated: true` | Met | `TestTenYears`, on #83's fixture (about 35,500 rows) |
| A query running past 2 s is cancelled | Met | A cross join of 10 years is cancelled with outcome `timeout` |
| Temp tables are gone after the transaction and no setting survives it | Met | `test_tables_role_and_settings_are_gone_afterwards` checks `current_user`, read-only, `statement_timeout` and `application_name` on the same connection, after a successful query, a failed one, and in-statement `set_config` (both local and session-level) |
| Totals from `transactions` and `monthly_totals` equal `/dashboard`'s | Met | `test_totals_agree_with_the_dashboard` (March 2020) |
| Amounts reach the model as decimal strings via `money.py`; no float | Met | `test_amounts_are_decimal_strings_never_floats`; `money.sql_major_units` |
| No query text or result in logs | Met | `test_no_query_text_or_result_is_logged` |
| Migration applies, reverses and re-applies; ruff and pytest green; `database` count grew | Met locally | `check_migrations.sh` passed; 733 → 793. CI still to run on the PR |

## Deviations and decisions

- **What runs is the deparsed statement**, written back out by `pglast` from the validated syntax tree. A comment or stray semicolon can't change the query once it's wrapped; the model's text never runs as-is.
- **`set_config` within the statement.** Under `SET LOCAL ROLE`, a statement can call `set_config('role', 'postgres')` and it succeeds, because the session's own user may switch back. The design copes:
  - the validator refuses `set_config`;
  - Postgres checks a statement's permissions before it runs, so a role change can't widen that same statement;
  - only one statement runs (a prepared statement refuses two);
  - nothing runs after it but the savepoint rollback, which undoes the change.

  Tests prove each point.
- **A savepoint inside the caller's transaction, not `BEGIN READ ONLY`.** Postgres refuses `CREATE` inside a read-only transaction, so the temp tables are created first and the transaction is then switched to `READ ONLY`. A savepoint lets tests run the sandbox inside their rolled-back test transaction. `handler_for` opens its own connection and transaction for production.
- **`budget_lines.spent`** is the category's spending that month from the monthly totals, which the database doesn't otherwise store.
- **`debts.interest_rate_bps`** stays in basis points, explained in the schema prompt, rather than being converted.
- **No functions in `FROM`** (for example `generate_series`): `RangeFunction` isn't allowed, which keeps the node allowlist small. The coach's examples don't need it.
- **Driver failures.** Reading `pg_stats` with the validator bypassed makes `asyncpg` fail to resolve the column types (`anyarray`), and it raises an internal client error rather than a database error. Every such failure now comes back as a `ToolError`, never a crash.
- **Downgrade drops `pg_trgm`** (RESTRICT), so the migration check's "schema empty" holds. On a database where something else already uses `pg_trgm`, the downgrade would refuse rather than break it.

### Changes from review (#88)

- **Schema-qualified operators are refused.** `OPERATOR(public.+)` was accepted before. Operator names now get the same rule as functions: `pg_catalog.` only.
- **The grant is explicit:** `WITH SET TRUE, INHERIT FALSE`.
  - CI and local run as a superuser, which may switch to any role, so they can't prove the grant.
  - A simulation with a non-superuser `CREATEROLE` login (as Supabase's `postgres` is) showed both the old and the new grant allow `SET LOCAL ROLE`. The change is for clarity and `INHERIT FALSE`, not a fix to a failure.
- **No switch can turn a wall off.** `run()` takes only the connection, household, currency and query; a test pins that signature. Tests replace a wall by patching `chat_sql.validate` or `chat_sql._ROLE_STATEMENT`, and `test_replacing_a_wall_really_removes_it` proves the patch takes effect.
- **The copy phase is bounded** by a 10-second `statement_timeout`. The model's query keeps its 2 seconds.
- **Array indexing is allowed:** `A_Indirection` and `A_Indices`, so `(array_agg(…))[1]` works.
- **Not changed: row order through the wrapper.** It relies on Postgres keeping a subquery's `ORDER BY` under `count(*) OVER ()` and `LIMIT`. That holds in practice, but SQL doesn't guarantee it, and no simple fix avoids the same assumption.

## Open questions and follow-ups

- **Before the production migration:**
  - confirm Render's `DATABASE_URL` user is `postgres.aldnrwcfcgesvyrmmhze`. The grant is to the migration's user, which must also be the app's;
  - after migrating, check by hand that the app's user can `SET ROLE finai_chat_reader` inside a rolled-back transaction.
- **Second reviewer:** the ticket asks for one beyond `/manager-review`, for the riskiest surface in the backend.
- **For 6.4:**
  - register `RUN_SQL` with `handler_for(...)` after the fixed tools;
  - put `SCHEMA_PROMPT` in the cached system prompt;
  - log which tool answered, so common `run_sql` questions can become fixed tools.
- **pgvector stays deferred**, as the ticket says. `merchants` plus `similarity()` covers fuzzy merchant matching for v1.
- **CI time:** the database suite now takes about 6 minutes locally, inside the 10-minute job limit but growing. The 10-year fixture is rebuilt per test; making it module-scoped would cut that.
