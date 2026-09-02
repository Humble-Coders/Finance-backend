# Handoff — ticket #10

**Ticket:** [#10 — \[M1\] Create the database schema and first migrations](https://github.com/Humble-Coders/Finance-backend/issues/10)
**Branch:** `ticket-10-database-schema` · **Base:** `main` · 23 files, +1879 / −2

## Summary

Eighteen tables across identity, money, categorization, planning, derived output and platform configuration, with the PRD's four locked constraints enforced by the **database** rather than by convention: UUID primary keys with no sequences, household scoping, integer minor units always paired with a currency, and source-agnostic transactions.

Four migrations: core schema, RLS on every table, the guarded `pgmq` queue, and seed data. All were applied to the real database, then downgraded to base and re-applied, so the round trip is proven rather than assumed. 130 tests pass — metadata rules that scan every table at once, plus constraint behaviour tested against a real Postgres inside a rolled-back transaction.

Four defects surfaced during the work, three of which would have blocked the next developer; all are fixed here and described below.

## Files changed

### Models — `app/models/` (9 files, ~830 lines)
| File | Why |
|---|---|
| `base.py` | The three locked rules as mixins, so no table can quietly opt out: UUID pk, timestamps, household FK+index. Plus `money_amount` / `money_currency` / `currency_check` helpers |
| `enums.py` | Seven native Postgres enums. `TransactionSource` includes `aggregator` from day one — the point of a source-agnostic table is that Phase 2 needs no schema change |
| `identity.py` | `Household`, `User`, `UserIdentity`. Carries the two constraints that stop one person becoming two households |
| `money.py` | `Account`, `Transaction`, `DocumentUpload`, including the dedup index and the document-retention columns |
| `categorization.py` | `Category` (system rows have `household_id` NULL, so **not** the NOT NULL mixin) and `CategoryCorrection` |
| `planning.py` | `Budget`, `BudgetLine`, `Goal`, `Debt`. Interest as **basis points**, integer — a rate in float drifts once compounded |
| `derived.py` | `HealthScoreSnapshot` (with `formula_version`, so tuning the formula doesn't silently rewrite past scores) and `ChatConversation` |
| `platform.py` | `SubscriptionEntitlement`, `CountryPack`, `FeatureAvailability`, `DisclaimerVersion` |
| `__init__.py` | Imports every model — autogenerate sees nothing that isn't imported |

### Migrations — `alembic/versions/` (4 files)
| File | Why |
|---|---|
| `fb76533e5c83_core_schema.py` | All 18 tables, indexes, constraints. Hand-edited after autogenerate (see Deviations) |
| `7ce291039fe7_enable_row_level_security.py` | RLS on every table, **no policies** — a table with RLS and no policy denies everything |
| `f5f0f4fbadb4_extraction_jobs_queue.py` | The queue, guarded twice: on the extension existing and the queue not existing |
| `ff7d60d4b3b8_seed_...py` | 20 system categories + the CA country pack, with deterministic ids and `ON CONFLICT DO NOTHING` |

### Infrastructure
| File | Why |
|---|---|
| `app/db.py` | Naming convention on the metadata — set **before** the first migration, or constraint names are unstable and `downgrade()` cannot drop them. Engine and sessionmaker created **lazily**, so importing models needs no configuration |
| `app/config.py` | `MIGRATION_DATABASE_URL` + `migration_dsn`, falling back to `DATABASE_URL` |
| `alembic/env.py` | Imports models for autogenerate; uses `migration_dsn` |
| `alembic/versions/.gitkeep` | The directory did not survive a clone (see Deviations) |
| `.env.example`, `.gitignore` | Documents the session-pooler URL and the percent-encoding trap; ignores `.idea/` |

### Tests
| File | Why |
|---|---|
| `test_models.py` | Metadata scans asserting the locked rules across **every** table — a new model cannot opt out |
| `test_constraints_integration.py` | Constraint behaviour against a real Postgres; SQLite implements none of the relevant features |
| `conftest.py` | Rolled-back transaction per test, so the suite can point at production without leaving data |
| `pytest.ini` | The `integration` marker and its rationale |

## How to test

```bash
git checkout ticket-10-database-schema
uv venv --python 3.12 && source .venv/bin/activate
uv pip install -r requirements-dev.txt
```

`.env` needs both DSNs — the second is the first with **6543 → 5432**:

```
DATABASE_URL=postgresql://postgres.<ref>:<password>@aws-0-us-east-1.pooler.supabase.com:6543/postgres
MIGRATION_DATABASE_URL=postgresql://postgres.<ref>:<password>@aws-0-us-east-1.pooler.supabase.com:5432/postgres
```

Percent-encode any `@` in the password as `%40`, or the URL parses part of it as the hostname.

```bash
alembic upgrade head --sql | less     # review before applying, per the ticket
alembic upgrade head
alembic upgrade head                  # again: the queue migration must be a no-op
pytest                                # 130 passed
alembic downgrade base && alembic upgrade head   # round trip
```

Verify in the database:

```sql
select count(*) from pg_tables where schemaname='public';                      -- 19 (18 + alembic_version)
select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace
  where n.nspname='public' and c.relkind='r' and not relrowsecurity;           -- 1 (alembic_version only)
select count(*) from pg_sequences where schemaname='public';                   -- 0
select count(*) from category where household_id is null;                      -- 20
select * from country_pack where country_code='CA';
select queue_name from pgmq.list_queues();                                     -- extraction_jobs
```

## Acceptance criteria

| Criterion | Status |
|---|---|
| `alembic upgrade head` applies cleanly through the pooler, no prepared-statement errors | ✅ Met — via the **session** pooler; see Deviations |
| `downgrade base` then `upgrade head` round-trips | ✅ Met — run end to end |
| `upgrade head --sql` produces reviewable SQL | ✅ Met — reviewed before applying |
| Every table has a UUID primary key; no sequences | ✅ Met — verified: 0 non-UUID pks, 0 sequences |
| Every financial table has `household_id` with FK and index | ✅ Met — asserted per-table by `test_models.py` |
| Money columns are BIGINT minor units + currency; no FLOAT/REAL/DOUBLE | ✅ Met — verified in the database and by metadata test |
| Inserting the same transaction twice raises an integrity error | ✅ Met — `TestTransactionDedup` |
| `household.country_code` accepts NULL | ✅ Met |
| Two providers may point at one user; duplicate `(provider, provider_user_id)` rejected | ✅ Met — `TestIdentity` |
| A second user with an already-used phone raises an integrity error | ✅ Met |
| RLS enabled on every table | ✅ Met — verified: 0 tables with RLS disabled |
| Re-running the queue migration where the queue exists succeeds | ✅ Met — run twice |
| Seeded categories and CA country pack present | ✅ Met — 20 categories, 1 pack |
| `pytest` passes; CI green | ⚠️ **Half met.** **130 pass locally.** There is *no CI* — `.github/workflows/` does not exist and ticket #13 has not started, so "CI green" cannot be true yet. Returns when #13 lands |

## Deviations / decisions

**1. `MIGRATION_DATABASE_URL` added — not in the ticket, but required.** DDL through the transaction pooler is cancelled by its statement timeout; even `CREATE TABLE alembic_version` failed with `QueryCanceledError`. Transaction mode hands each statement to whichever backend is free and is not built for schema changes. Alembic now uses a session-mode connection (5432) while the app keeps the transaction pooler (6543). It falls back to `DATABASE_URL`, so a plain Postgres — CI in ticket #15 — needs no extra configuration.

**2. `alembic/versions/` was never committed.** Git does not track empty directories, so the scaffold's migrations folder vanished on clone and the first `alembic revision` failed. A `.gitkeep` fixes it for everyone.

**3. The autogenerated `downgrade()` did not drop the enum types.** `drop_table` leaves Postgres enums behind, so `downgrade base` then `upgrade head` failed with "type already exists" — the exact criterion the ticket asks for. Explicit `DROP TYPE IF EXISTS` added for all seven.

**4. A `CheckConstraint` passed to `mapped_column` is silently discarded.** The currency-length checks never reached the metadata; the database accepted `'CA'` where `'CAD'` belongs, silently mislabelling money. Moved to table-level `__table_args__`, with a metadata test asserting every currency column has one so it cannot regress quietly.

**5. The integration tests initially skipped.** The guard read `os.getenv("DATABASE_URL")`, but the DSN comes from `.env` via pydantic-settings — a green run that proved nothing. Now resolved through `Settings`.

**6. Native Postgres enums rather than VARCHAR + CHECK.** Real integrity at the database level. Adding a value later is `ALTER TYPE ... ADD VALUE`; removing one needs a type rewrite, so prefer adding.

**7. `Category` does not use the household mixin.** System categories are shared and have `household_id` NULL, which a NOT NULL mixin cannot express. A partial unique index on `(household_id, slug)` still prevents duplicates per household.

**8. Interest stored as basis points**, integer — for the same reason money is not float: a rate in floating point drifts once compounded over a repayment schedule.

## Manager review — changes applied

Review of PR #16 found one real gap and two weak tests. All fixed on this branch:

1. **Duplicate system categories were accepted.** `uq_category_household_slug` covers `(household_id, slug)`, but Postgres treats NULLs as distinct — so it constrained user-defined categories and left system rows (`household_id NULL`) unconstrained. A second system "groceries" inserted successfully, and every household would have seen two. Closed with a **partial unique index** (`uq_category_system_slug ... WHERE household_id IS NULL`) in migration `1daf2b084378`, plus two tests: the duplicate is now rejected, and a household can still define its own `groceries`.
2. **`pytest.raises(Exception)` narrowed to `CheckViolationError`.** The broad form would have passed on a typo in the INSERT — proving nothing — and this is the test that caught the discarded CheckConstraint.
3. **"CI green" unticked.** There is no CI yet (#13).

### Second review pass

Re-review confirmed the fixes with a check not run the first time — `alembic revision --autogenerate` against the live database produced **zero operations**, so models and applied schema agree exactly. That validates every hand-edit at once: the enum drops, the seven currency checks and the partial index.

Two further gaps were found and closed, both about tests failing to catch *future* regressions rather than anything wrong today:

4. **RLS coverage would have degraded silently.** The RLS migration hardcodes an 18-table list, so a table added by a future migration gets none — and nothing caught it. Metadata tests structurally cannot: RLS is database state, not schema metadata. Added `TestRowLevelSecurity`, which asserts every public table has `relrowsecurity` and that no permissive policy exists. For a defence-in-depth control, "we remembered last time" is not a mechanism.
5. **The household-scoping test silently exempted three tables.** `user`, `subscription_entitlement` and `category` all carry `household_id`, but sat in one exclusion set alongside genuinely unscoped tables — so the FK-and-index assertion skipped them while reading as though it covered everything. Split into `NO_HOUSEHOLD` / `HOUSEHOLD_VIA_PARENT` / `HOUSEHOLD_NULLABLE`, plus a test asserting the exclusion lists are honest.

6. **The "needs no database" tests could not run without a database configured.** `app/db.py` built the engine at **import time**, so importing the ORM models triggered `Settings` validation and raised `ValidationError: database_url Field required` before a single test was collected. The claim that 117 tests need no database was therefore false as written — they need no *connection*, but did need config. Worse, ticket #13 would have gone red on its first run, and the obvious "fix" would have been dummy env vars in the workflow, papering over an import-time side effect. The engine is now created lazily on first use. Verified by running the suite with `.env` removed and the variables unset: **117 passed, 13 skipped, 0.24s**. A side benefit: the full suite dropped from ~195s to ~56s, because the engine is no longer rebuilt repeatedly.

**The test fixture was also rebuilt.** It opened a fresh connection per test, which exhausted the pooler once the suite grew (`ECHECKOUTTIMEOUT`, then `authentication did not complete`). It now shares one connection for the whole session, wrapped in a per-test transaction that always rolls back — and uses the **runtime** DSN rather than the migration one, since these tests only do DML and the small session pool is needed for migrations. A side benefit: the tests now exercise the same connection path the application uses.

## Open questions / follow-ups

- **`describe_dsn` reports a host SQLAlchemy is not using** when a password contains `@`. `urlsplit` splits userinfo at the last `@` per RFC 3986; SQLAlchemy's `make_url` splits at the first. The diagnostic built to debug connection problems lied during exactly such a problem. Deliberately not fixed here to keep this PR to the schema — worth its own small ticket.
- **The integration suite takes about a minute** against a cross-region database, while the 117 unit tests run in 0.23s. Ticket #15 should keep them as separate jobs so a slow database check never gates fast feedback.
- **CI cannot run the constraint tests.** Thirteen tests skip without a database, so the ticket's most important criteria are unproven in CI. Ticket #15 adds Postgres to CI and should also add `MIGRATION_DATABASE_URL` (or rely on the fallback).
- **Migrations still run against production** — there is no staging. It was safe here because there is no user data, and the destructive round trip was run deliberately while that is still true. That window is closing.
- **`transaction.normalized_description` is part of the dedup key** but nothing produces it yet. Ticket #11/M3 must normalize deterministically, or dedup silently stops working.
- **No `pgvector` column yet.** The extension is enabled but the merchant embedding index arrives with M6.
