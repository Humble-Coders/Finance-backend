# Handoff — ticket #13

**Ticket:** [#13 — \[M1\] Add a CI workflow](https://github.com/Humble-Coders/Finance-backend/issues/13)
**Branch:** `ticket-13-ci-workflow` · **Base:** `main`

## Summary

A GitHub Actions workflow that runs on every pull request and push to `main`: install dependencies on the pinned interpreter, lint, check formatting, run the tests. No repository secrets and no database.

Adding `ruff` configuration surfaced 49 violations, fixed in a separate commit so the twelve-line workflow stays reviewable. Verified end to end on this PR — green, then deliberately broken to confirm it goes red, then reverted.

## Files changed

18 files, +340 / −85, across five commits.

**New**
| File | Why |
|---|---|
| `.github/workflows/ci.yml` | The workflow. Interpreter read from `.python-version` rather than hardcoded, so CI cannot drift from what Render deploys |
| `pyproject.toml` | Ruff configuration — rules stated explicitly rather than inherited from whatever ruff ships next |
| `handoffs/ticket-13.md` | This report |

**Modified**
| File(s) | Why |
|---|---|
| `README.md` | CI status badge — the only substantive line among the modified files |
| `tests/test_constraints_integration.py` | Eight `E501`s fixed by rewrapping SQL into triple-quoted blocks; `ruff format` cannot reflow string literals |
| `tests/test_models.py` | `not x is True` → `is not True` (ruff `E714`, and it was right), plus reflow |
| 12 further files — `alembic/env.py`, `app/api/capabilities.py`, `app/core/money.py`, `app/db.py`, `app/models/*`, `tests/test_dsn.py` | **Formatting only** — import splitting and sorting, line wrapping, `__all__` one entry per line. All in commit `09367c7`; skim rather than read |

**No behaviour changed in the reformatted files.** Verified after the fact: the same 25 names are exported from `app.models` and the same 18 tables are registered on the metadata as before.

**Commit sequence** — `09367c7` ruff config + fixes → `badb15f` the workflow → `5ea6933` deliberate breakage → `7d5a387` revert → this report. The middle two cancel out in the net diff; they exist so the run history proves the pipeline can fail.

## How to test

```bash
ruff check . && ruff format --check . && pytest -q
```

Expect `All checks passed!` and `121 passed, 13 deselected`.

For the workflow itself: open any PR against `main` and watch the **CI** check.

## Acceptance criteria

| Criterion | Status |
|---|---|
| Opening a PR runs the workflow automatically | ✅ Met — ran on this PR |
| Installs on **Python 3.12.8**, version visible in the log | ✅ Met — `Successfully set up CPython (3.12.8)` |
| `pytest` runs and passes; a deliberately broken test makes CI red | ✅ Met — run history reads **success → failure → success**. The failure was `AssertionError: assert None` on the broken assertion, then reverted |
| `ruff check` and `ruff format --check` pass | ✅ Met — `All checks passed!` |
| No repository secrets; does not contact the database | ✅ Met — `117 passed, 13 skipped` on a clean runner with no configuration |
| README badge reflects real status | ✅ Met |
| A full run completes in under about two minutes | ✅ Met incidentally — **26 seconds** — but treated as a note, not a gate (see below) |

## Deviations / decisions

**1. The interpreter is read from `.python-version`, not hardcoded.** A literal `3.12.8` in the workflow would be a second place to update, and the entire reason this ticket exists is that CI must not drift from what deploys — that drift is what killed the first deploy (Render chose 3.14, which has no `pydantic-core` wheels).

**2. Ruff rules are `E`/`F` plus `I`.** `ruff format` does not sort imports, so without `I` the order drifts and produces diff noise that hides real changes. Bugbear, pyupgrade and friends were deliberately left off: they would have ballooned the cleanup and buried the workflow in an unrelated diff.

**3. Formatting is a separate commit.** 15 files changed. Mixing that with the workflow would have made the workflow unreviewable.

**4. `alembic/versions` is excluded from linting.** Migrations are largely generated, and reformatting them would churn files nobody edits by hand.

**5. Eight `E501` violations were fixed by rewrapping SQL, not by suppressing the rule.** `ruff format` cannot reflow string literals. The SQL reads better as triple-quoted blocks regardless.

**6. "Under two minutes" is treated as a note rather than a gate.** It is an arbitrary threshold that will be the first thing to fail as the suite grows, and failing it would not indicate anything is wrong. The run takes 26 seconds today, so the point is moot — but it is flagged rather than silently dropped.

**7. `concurrency` cancels superseded runs** on the same branch. Not in the ticket; it costs nothing and stops a queue of stale runs.

## Manager review — change applied

Review found that **CI could not catch the failure class this ticket exists to prevent.** Nothing in the suite imported `app.main` or `app.workers.main`, so an import-time break in either passed CI and failed on deploy — exactly deploy failure #2 (`ModuleNotFoundError: psycopg2`).

Measured before fixing, with a deliberately broken `app/main.py`:

| Injected fault | `pytest` | `import app.main` |
|---|---|---|
| Invalid syntax | 117 passed | fails |
| Missing module, unused import | 117 passed | `ModuleNotFoundError` |
| Missing module, import **used** | 117 passed | `ModuleNotFoundError` |

Ruff caught the first two incidentally (syntax error; unused import) but **does not resolve imports**, so the third — a used import of a nonexistent module — was invisible to every check while breaking the deploy.

`tests/test_entrypoints.py` closes it: both deployed entrypoints must import, and the attributes the start commands depend on must exist (`app` for `uvicorn app.main:app`, `Worker` for `python -m app.workers.main`) — importing alone would let a rename pass while still breaking startup. Re-injecting the same break still produces failures. **121 tests pass.**

**The new test immediately failed on CI, correctly.** `app/main.py:12` calls `get_settings()` at module level to decide whether to expose `/docs`, so importing it requires configuration — the same import-time-config defect already fixed in `app/db.py`, in a second place, found by the very test added to catch import failures. It passed locally only because a `.env` exists.

The fix was neither to weaken `Settings` (which should keep requiring `DATABASE_URL` in production) nor to put dummy values in the workflow (which would hide the coupling in CI config, where nobody would look). Unlike `app.models` — which must import with no configuration, because schema tests need that — a deployed app is legitimately configured at import. So the tests supply throwaway values via `monkeypatch` and clear the settings cache on both sides. Nothing connects; the engine is lazy.

Verified with `.env` removed and the environment cleared: **121 passed, 13 skipped**.

## Open questions / follow-ups

- **CI does not make the check required.** A red run blocks nothing until branch protection is configured on `main` — a repository setting, and the manager's call, not something this ticket can do. Worth doing now that the check exists and is proven.
- **The 13 database-backed tests still skip in CI.** That is by design here; **ticket #15** adds Postgres and turns them on. Until then the constraint behaviour from #10 is unverified in CI.
- **No `mypy`, no coverage threshold, no dependency scanning.** Each is a reasonable addition and each is its own decision; none belong in a first pass.
- **This ticket would have caught the lazy-engine defect** found by hand in #16 — importing the models raised `ValidationError` with no configuration. That is exactly this class of failure, and it would have made the first run red. An argument for #13 having gone first.
