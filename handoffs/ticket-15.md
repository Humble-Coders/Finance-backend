# Handoff — ticket #15

**Ticket:** [#15 — \[M1\] Test migrations against a real Postgres in CI](https://github.com/Humble-Coders/Finance-backend/issues/15)
**Branch:** `ticket-15-migration-ci` · **Base:** `main` (`10a7d5d`) · **PR:** #21 · 5 files, +251 / −8

## Summary

CI gains a `database` job that starts a throwaway Postgres 17 + pgmq 1.5.1 container — matching production — and runs `scripts/check_migrations.sh` against it: every migration applies to an empty database, a repeat `upgrade head` changes nothing, `downgrade base` leaves nothing behind, and a second `upgrade head` reproduces the schema byte for byte. The same job then runs the **85 database tests**, which CI previously skipped entirely. Until now production was the first place any migration ever ran; now every PR proves the migrations on a disposable copy first.

The script is shared by CI and local rehearsal, and refuses to touch any database that is not on `localhost` — a developer `.env` points at production, and this script drops every table. `REQUIRE_DB` makes a missing database fail the job instead of skipping every test, because a fully skipped suite reports green.

On CI the whole thing takes **48s**, running alongside the existing 23s `test` job — well inside the ticket's five-minute budget. It validates migration **correctness**, not survival of Supabase's transaction pooler; the workflow says so explicitly.

## Files changed

**CI**
| File | Why |
|---|---|
| `.github/workflows/ci.yml` | New `database` job: pinned `ghcr.io/pgmq/pg17-pgmq:v1.5.1` service container, no secrets, 10-minute timeout, runs the migration check then `pytest -m integration`. Comments state what a green run does and does not prove. Stale "13 tests skip" comment in the `test` job corrected |

**Tooling**
| File | Why |
|---|---|
| `scripts/check_migrations.sh` *(new, executable)* | The migration check itself — localhost guard, pgmq enablement, single-head check, upgrade / repeat / downgrade / re-upgrade with schema-dump comparisons and a leftover-objects check |

**Test harness**
| File | Why |
|---|---|
| `tests/conftest.py` | `pytest_configure` raises when `REQUIRE_DB` is set but no database is configured; docstring updated for CI |
| `pytest.ini` | `integration` marker description no longer says CI skips these |

**Docs**
| File | Why |
|---|---|
| `CLAUDE.md` | Testing section: migrations must round-trip; how to rehearse locally, and why the script refuses non-localhost |

## How to test

**On the PR** — open or push, and both `test` and `database` run. The `database` job log ends with `All migration checks passed` and `85 passed`.

**Locally** (needs Docker; nothing else in the project does):

```bash
git checkout ticket-15-migration-ci
docker run -d --name finai-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 ghcr.io/pgmq/pg17-pgmq:v1.5.1
export DATABASE_URL=postgresql://postgres:postgres@localhost:55432/postgres
PG_CONTAINER=finai-pg PYTHON=.venv/bin/python scripts/check_migrations.sh
REQUIRE_DB=1 .venv/bin/python -m pytest -q -m integration     # expect 85 passed
docker rm -f finai-pg
```

`DATABASE_URL` in the environment overrides `.env`, so neither command reaches production; the script additionally pins `MIGRATION_DATABASE_URL` and checks both resolved hosts before doing anything.

**The guard** — point it anywhere non-local and it must refuse before touching Docker or Alembic:
```bash
DATABASE_URL=postgresql://u:p@example.invalid/db PG_CONTAINER=x scripts/check_migrations.sh   # exit 1
```

## Acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| Opening a pull request runs the migration job automatically | ✅ Met | `on: pull_request`; run [34458642174](https://github.com/Humble-Coders/Finance-backend/actions/runs/34458642174) triggered by `pull_request` |
| `upgrade head` → `downgrade base` → `upgrade head` completes green | ✅ Met | Final run [34459157610](https://github.com/Humble-Coders/Finance-backend/actions/runs/34459157610): `All migration checks passed in 6s` |
| Running `upgrade head` twice is a no-op the second time | ✅ Met | Script compares the revision **and** a schema dump before and after the second run |
| A deliberately broken `downgrade()` makes CI red (verify once, then revert) | ✅ Met | `4729850` → run [34458832633](https://github.com/Humble-Coders/Finance-backend/actions/runs/34458832633) red: `index "uq_does_not_exist" does not exist`; reverted in `e20eb37` |
| Two divergent migration heads make CI red | ✅ Met | `35e82a8` → run [34459005058](https://github.com/Humble-Coders/Finance-backend/actions/runs/34459005058) red: `expected one head, found 2`; removed in `9407d51` |
| The job needs no repository secrets | ✅ Met | The job's env holds only a literal localhost URL and a placeholder `SUPABASE_URL`; no `secrets.` reference anywhere in the job |
| Total CI time stays under about five minutes | ✅ Met | `database` 48s, `test` 23s, in parallel |

Both red runs failed in the `database` job at the migration step, with the `test` job green — so each red is attributable to its one cause. The same breakages were also rehearsed locally first, plus a third the ticket does not ask for: a downgrade that drops its tables but leaves enum types behind (the #10 bug) is caught by the leftover check.

## Deviations / decisions

**1. The 85 database tests now run in CI too.** Not in the ticket's scope list; agreed with the manager at kickoff. `pytest.ini` already promised it ("ticket #15 turns them on"), and every defect found during #12 lived in those tests. Against a local container they take ~5s rather than the ~16 minutes they take against the cross-region production database.

**2. No pgvector.** The ticket asks for `pgmq` and `vector`. No migration uses `vector` yet, so the image carries only pgmq; the workflow notes that the first migration needing it must change the image. Production has pgvector 0.8.2.

**3. The image matches production's major version, not its patch.** `pg17-pgmq:v1.5.1` runs Postgres **17.4**; production runs **17.6**, both with pgmq 1.5.1. Pinned rather than `:latest` so it cannot drift silently.

**4. Stronger checks than the criteria require.** "`upgrade head` twice is a no-op" is nearly vacuous on its own — the second run finds the database at head and executes nothing. So the script also dumps the schema after each stage, fails if `downgrade base` leaves any table, enum, sequence, view or function behind, and fails if the round trip does not reproduce the first schema exactly. The round trip is where the queue migration actually meets an existing queue — the guard production depends on — because its `downgrade()` deliberately leaves the queue in place.

**5. pgmq is enabled explicitly.** The image makes pgmq *available*; the queue migration checks whether it is *installed*. Without `CREATE EXTENSION pgmq` its real path would have been skipped here just as on plain Postgres. The script also asserts exactly one queue exists after the first upgrade, so that path is proven to run rather than assumed.

**6. "Fail on any Alembic warning about multiple heads"** is implemented as counting heads and failing on anything but one, with both heads named in the message — more direct than parsing warnings.

**7. The safety guard resolves the database the app will really use.** A developer `.env` sets `MIGRATION_DATABASE_URL` to production, and Alembic prefers it — so exporting `DATABASE_URL=localhost` alone would still have migrated production. The script pins `MIGRATION_DATABASE_URL` to the throwaway database and then checks both hosts through the app's own `Settings`, not by reading the variable.

**8. Two defects found and fixed while building it:**
- The first push never ran: GitHub rejected the workflow file — `Unrecognized named-value: 'job'` — because `job.services.postgres.id` was in the job-level `env`, where the `job` context does not exist ([run 34458250656](https://github.com/Humble-Coders/Finance-backend/actions/runs/34458250656), zero jobs). Moved onto the step. Local rehearsal could not have caught it: it exercises the script, not the workflow wiring.
- `docker exec -i` in the script attached stdin and swallowed the rest of a heredoc-driven caller. Harmless in CI, a trap anywhere else; `-i` removed.

**9. The temporary red commits stay in history** (`4729850`, `35e82a8` and their reverts) as the evidence for criteria 4 and 5. The branch's final tree is byte-identical to `024dd4e`. A squash merge would drop them if you prefer a cleaner `main`.

## Open questions / follow-ups

- **Branch protection on `main` is still unset, so both jobs remain advisory.** The two red runs above would *not* have blocked a merge. Requiring `test` and `database` is what turns this ticket into a gate. A repository setting — yours to change, or say the word.
- **The pooler remains untested in CI**, by design and as the ticket anticipated. `/readyz` on the deployed service is still the only check that migrations and queries survive Supabase's transaction pooler.
- **No staging environment.** This removes "production is the first place a migration runs"; production is still the only real one.
- **Keep the image in step with production** — bump it when Supabase upgrades production's Postgres or pgmq.
- **pgvector** — the first migration that uses it needs an image that provides it.
- **`actionlint` would have caught the invalid workflow before it was pushed.** Neither it nor `shellcheck` runs anywhere today; both are cheap CI steps to add.
- Local rehearsal needs Docker. It is optional — CI needs nothing from developers — but it is how to iterate on a migration without touching production.
