# Handoff — ticket #13

**Ticket:** [#13 — \[M1\] Add a CI workflow](https://github.com/Humble-Coders/Finance-backend/issues/13)
**Branch:** `ticket-13-ci-workflow` · **Base:** `main`

## Summary

A GitHub Actions workflow that runs on every pull request and push to `main`: install dependencies on the pinned interpreter, lint, check formatting, run the tests. No repository secrets and no database.

Adding `ruff` configuration surfaced 49 violations, fixed in a separate commit so the twelve-line workflow stays reviewable. Verified end to end on this PR — green, then deliberately broken to confirm it goes red, then reverted.

## Files changed

| File | Why |
|---|---|
| `.github/workflows/ci.yml` | The workflow. Interpreter read from `.python-version` rather than hardcoded, so CI cannot drift from what Render deploys |
| `pyproject.toml` | Ruff configuration — rules stated explicitly rather than inherited from whatever ruff ships next |
| `README.md` | CI status badge |
| 15 files reformatted + lint fixes | Separate commit `09367c7`; skim rather than read |

## How to test

```bash
ruff check . && ruff format --check . && pytest -q
```

Expect `All checks passed!` and `117 passed, 13 deselected`.

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

## Open questions / follow-ups

- **CI does not make the check required.** A red run blocks nothing until branch protection is configured on `main` — a repository setting, and the manager's call, not something this ticket can do. Worth doing now that the check exists and is proven.
- **The 13 database-backed tests still skip in CI.** That is by design here; **ticket #15** adds Postgres and turns them on. Until then the constraint behaviour from #10 is unverified in CI.
- **No `mypy`, no coverage threshold, no dependency scanning.** Each is a reasonable addition and each is its own decision; none belong in a first pass.
- **This ticket would have caught the lazy-engine defect** found by hand in #16 — importing the models raised `ValidationError` with no configuration. That is exactly this class of failure, and it would have made the first run red. An argument for #13 having gone first.
