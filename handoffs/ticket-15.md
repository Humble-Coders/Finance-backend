# Handoff — ticket #15

**Ticket:** [#15 — \[M1\] Test migrations against a real Postgres in CI](https://github.com/Humble-Coders/Finance-backend/issues/15)
**Branch:** `ticket-15-migration-ci` · **Base:** `main` (`10a7d5d`) · **PR:** #21 · 6 files

## Summary

CI gains a `database` job that starts a throwaway Postgres 17 + pgmq 1.5.1 container — matching production — and runs `scripts/check_migrations.sh` against it: every migration applies to an empty database, a repeat `upgrade head` changes nothing, `downgrade base` leaves nothing behind, and a second `upgrade head` reproduces the schema byte for byte. The same job then runs the **85 database tests**, which CI previously skipped entirely. Until now production was the first place any migration ever ran; now every PR proves the migrations on a disposable copy first.

The script is shared by CI and local rehearsal. Because it drops every table and a developer `.env` points at production, it refuses to run unless its connection string names `localhost`, carries no host override, and reaches the *same server* as the container it inspects — compared by Postgres's system identifier, not by hostname. `REQUIRE_DB` makes a missing database fail the job instead of skipping every test, because a fully skipped suite reports green.

On CI the whole thing takes **50s**, running alongside the existing 27s `test` job — well inside the ticket's five-minute budget. It validates migration **correctness**, not survival of Supabase's transaction pooler; the workflow says so explicitly.

## Files changed

**CI**
| File | Why |
|---|---|
| `.github/workflows/ci.yml` | New `database` job: `ghcr.io/pgmq/pg17-pgmq:v1.5.1` service container pinned by tag and digest, TCP health check, no secrets, 10-minute timeout, runs the migration check then `pytest -m integration`. Comments state what a green run does and does not prove. Stale "13 tests skip" comment in the `test` job corrected |

**Tooling**
| File | Why |
|---|---|
| `scripts/check_migrations.sh` *(new, executable)* | The migration check itself — a guard that the DSN is local and reaches the same server as `PG_CONTAINER`, pgmq enablement, single-head check, upgrade / repeat / downgrade / re-upgrade with schema-dump comparisons and a leftover-objects check |

**Test harness**
| File | Why |
|---|---|
| `tests/conftest.py` | `pytest_configure` raises when `REQUIRE_DB` is set but no database is configured; docstring updated for CI |
| `pytest.ini` | `integration` marker description no longer says CI skips these |

**Docs**
| File | Why |
|---|---|
| `CLAUDE.md` | Testing section: migrations must round-trip; how to rehearse locally, and what the script verifies before it will run |

## How to test

**On the PR** — open or push, and both `test` and `database` run. The `database` job log ends with `All migration checks passed` and `85 passed`.

**Locally** (needs Docker; nothing else in the project does):

```bash
git checkout ticket-15-migration-ci
docker run -d --name finai-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 ghcr.io/pgmq/pg17-pgmq:v1.5.1@sha256:e6f893a793751ed30c89f5f88e95aa52b77c1a03440b7d118a996866489ac0c6
export DATABASE_URL=postgresql://postgres:postgres@localhost:55432/postgres
PG_CONTAINER=finai-pg PYTHON=.venv/bin/python scripts/check_migrations.sh
REQUIRE_DB=1 .venv/bin/python -m pytest -q -m integration     # expect 85 passed
docker rm -f finai-pg
```

`DATABASE_URL` in the environment overrides `.env`, so neither command reaches production; the script additionally pins `MIGRATION_DATABASE_URL`, and before doing anything checks that the DSN reaches the same server as the container.

**The guard** — with the container above running and the venv active, each of these must refuse before Alembic runs:
```bash
DATABASE_URL=postgresql://u:p@example.invalid/db PG_CONTAINER=finai-pg scripts/check_migrations.sh                                      # not localhost
DATABASE_URL='postgresql://postgres:postgres@localhost:55432/postgres?host=example.invalid' PG_CONTAINER=finai-pg scripts/check_migrations.sh  # host override
DATABASE_URL=postgresql://postgres:postgres@localhost:55432/postgres PG_CONTAINER=no-such-container scripts/check_migrations.sh           # container unreachable
```

## Acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| Opening a pull request runs the migration job automatically | ✅ Met | `on: pull_request`; run [34458642174](https://github.com/Humble-Coders/Finance-backend/actions/runs/34458642174) triggered by `pull_request` |
| `upgrade head` → `downgrade base` → `upgrade head` completes green | ✅ Met | Run [34460812452](https://github.com/Humble-Coders/Finance-backend/actions/runs/34460812452) on `8e31e5a`, the last change to code: `All migration checks passed in 5s`, `85 passed`. Later commits touch only documentation and comments; CI runs on every push |
| Running `upgrade head` twice is a no-op the second time | ✅ Met | Script compares the revision **and** a schema dump before and after the second run |
| A deliberately broken `downgrade()` makes CI red (verify once, then revert) | ✅ Met | `4729850` → run [34458832633](https://github.com/Humble-Coders/Finance-backend/actions/runs/34458832633) red: `index "uq_does_not_exist" does not exist`; reverted in `e20eb37`. Made before the review round rewrote the guard; the same breakage re-verified locally after it |
| Two divergent migration heads make CI red | ✅ Met | `35e82a8` → run [34459005058](https://github.com/Humble-Coders/Finance-backend/actions/runs/34459005058) red: `expected one head, found 2`; removed in `9407d51`. Same caveat — re-verified locally after the rewrite |
| The job needs no repository secrets | ✅ Met | The job's env holds a literal localhost URL, a placeholder `SUPABASE_URL` and `REQUIRE_DB`; no `secrets.` reference anywhere in the workflow |
| Total CI time stays under about five minutes | ✅ Met | `database` 50s, `test` 27s, in parallel (run 34460812452) |

Both red runs failed in the `database` job at the migration step, with the `test` job green — so each red is attributable to its one cause. The same breakages were also rehearsed locally first, plus a third the ticket does not ask for: a downgrade that drops its tables but leaves enum types behind (the #10 bug) is caught by the leftover check.

## Deviations / decisions

**1. The 85 database tests now run in CI too.** Not in the ticket's scope list; agreed with the manager at kickoff. `pytest.ini` already promised it ("ticket #15 turns them on"), and every defect found during #12 lived in those tests. Against a local container they take ~5s rather than the ~16 minutes they take against the cross-region production database.

**2. No pgvector.** The ticket asks for `pgmq` and `vector`. No migration uses `vector` yet, so the image carries only pgmq; the workflow notes that the first migration needing it must change the image. Production has pgvector 0.8.2.

**3. The image matches production's major version, not its patch.** `pg17-pgmq:v1.5.1` runs Postgres **17.4**; production runs **17.6**, both with pgmq 1.5.1. Pinned by tag and content digest — in CI and in the local-rehearsal instructions alike — so neither can drift silently.

**4. Stronger checks than the criteria require.** "`upgrade head` twice is a no-op" is nearly vacuous on its own — the second run finds the database at head and executes nothing. So the script also dumps the schema after each stage, fails if `downgrade base` leaves any table, enum, sequence, view or function behind, and fails if the round trip does not reproduce the first schema exactly. The round trip is where the queue migration actually meets an existing queue — the guard production depends on — because its `downgrade()` deliberately leaves the queue in place.

**5. pgmq is enabled explicitly.** The image makes pgmq *available*; the queue migration checks whether it is *installed*. Without `CREATE EXTENSION pgmq` its real path would have been skipped here just as on plain Postgres. The script also asserts exactly one queue exists after the first upgrade, so that path is proven to run rather than assumed.

**6. "Fail on any Alembic warning about multiple heads"** is implemented as counting heads and failing on anything but one, with both heads named in the message — more direct than parsing warnings.

**7. The safety guard resolves the database the app will really use.** A developer `.env` sets `MIGRATION_DATABASE_URL` to production, and Alembic prefers it — so exporting `DATABASE_URL=localhost` alone would still have migrated production. The script pins `MIGRATION_DATABASE_URL` to the throwaway database and then checks both hosts through the app's own `Settings`, not by reading the variable. Review found a hostname check alone was not enough — see *Review round* below.

**8. Two defects found and fixed while building it:**
- The first push never ran: GitHub rejected the workflow file — `Unrecognized named-value: 'job'` — because `job.services.postgres.id` was in the job-level `env`, where the `job` context does not exist ([run 34458250656](https://github.com/Humble-Coders/Finance-backend/actions/runs/34458250656), zero jobs). Moved onto the step. Local rehearsal could not have caught it: it exercises the script, not the workflow wiring.
- `docker exec -i` in the script attached stdin and swallowed the rest of a heredoc-driven caller. Harmless in CI, a trap anywhere else; `-i` removed.

**9. The temporary red commits stay in history** (`4729850`, `35e82a8` and their reverts) as the evidence for criteria 4 and 5. The branch's final tree is byte-identical to `024dd4e`. A squash merge would drop them if you prefer a cleaner `main`.

## Review round — fixes applied

`/manager-review 21` found that the script's safety rested on a hostname, and that two of its checks could pass on failure. Every finding was reproduced before fixing and re-run after.

**1. The checks and the migration could target different databases.** Every check ran through `docker exec` into `PG_CONTAINER`, while Alembic connected through the DSN, and nothing tied the two together. With the DSN on container A and `PG_CONTAINER` on B, `upgrade head` migrated A while every check inspected B (0 tables); an unrelated queue assertion stopped it only *after* the migration. A localhost port-forward to production plus a pending migration would have migrated production — the #19/#20 incident again.

**2. The localhost guard was bypassable.** It compared `make_url(dsn).host`, but `normalize_async_dsn` keeps a `?host=` query parameter and asyncpg obeys it: the guard printed *ok* while the engine dialled `example.invalid`.

*One fix for both:* before anything else, the script reads `pg_control_system().system_identifier` through the same SQLAlchemy connection path Alembic uses — so a query-string host is followed exactly as Alembic would follow it — and through `docker exec`, and refuses unless they match. It also rejects `host=` / `hostaddr=` in the query outright, for a clear message.

**3. The empty-database check failed open.** In `[ -z "$(psql_c …)" ]`, a failing `$(…)` inside `[ ]` does not trip `set -e`, so an unreachable container read as "empty" and the next step blamed the image for lacking pgmq. Now captured by assignment, which fails closed; the pgmq message no longer guesses a cause.

**4. The health check could pass during first boot.** `pg_isready` over the socket answers the image's temporary initdb server (`listen_addresses=''`), which then restarts. Observed, not inferred: a sample caught `socket=up tcp=down`, followed in the container log by *shutting down → init process complete → ready*. Masked in CI by ~20s of setup steps. Now checked over TCP (`-h 127.0.0.1`), which only the real server answers.

**5. The image is pinned by digest as well as tag**, because a tag can be re-pointed at different content.

**Re-verified locally after the fixes:**

| Case | Result |
|---|---|
| DSN → container A, `PG_CONTAINER` → B | refused — different system identifiers; **A never migrated** |
| `?host=example.invalid` | refused — host override in the query string |
| Non-local host | refused |
| `PG_CONTAINER` not running | refused — cannot query `PG_CONTAINER` |
| Normal run | green in 4s; 85 database tests pass |
| Broken `downgrade()` / forked chain / leftover enums | still red, each with its own message |

**Also corrected:** this report, `CLAUDE.md` and the script header all said the script "refuses any non-localhost database". A hostname was all it checked, and even that was bypassable. They now say what is actually verified. The header's line counts are gone too — they went stale with this report's own commit.

### Second review

No correctness or security issues in the round-1 fixes: CI confirmed the identity check (`the DSN reaches the same server as …`), the TCP health check and the digest pull, and a failed login does not echo the password into the output. Two corrections: the local-rehearsal instructions still used the image by tag alone, so a local run could drift from CI's digest-pinned image — now pinned in both places; and this report's evidence cited runs from before the fixes — refreshed to run 34460812452 on `8e31e5a`.

## Open questions / follow-ups

- **Branch protection on `main` is still unset, so both jobs remain advisory.** The two red runs above would *not* have blocked a merge. Requiring `test` and `database` is what turns this ticket into a gate. A repository setting — yours to change, or say the word.
- **The pooler remains untested in CI**, by design and as the ticket anticipated. `/readyz` on the deployed service is still the only check that migrations and queries survive Supabase's transaction pooler.
- **No staging environment.** This removes "production is the first place a migration runs"; production is still the only real one.
- **Keep the image in step with production** — bump it when Supabase upgrades production's Postgres or pgmq.
- **pgvector** — the first migration that uses it needs an image that provides it.
- **`actionlint` would have caught the invalid workflow before it was pushed.** Neither it nor `shellcheck` runs anywhere today; both are cheap CI steps to add.
- Local rehearsal needs Docker. It is optional — CI needs nothing from developers — but it is how to iterate on a migration without touching production.
