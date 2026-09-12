# Handoff — ticket #29

**Ticket:** [#29 — \[M2\] Make the core financial setup mandatory](https://github.com/Humble-Coders/Finance-backend/issues/29)
**Branch:** `ticket-29-mandatory-financial-setup` · **Base:** `main` (`45d7373`) · **Implementation:** `ffa66ed`, `28e0b63` · **PR:** #30 · 13 files (10 implementation, 3 documentation)

## Summary

Monthly income and monthly expense become mandatory to reach the app. They are
reported as a fourth onboarding step, `financial_setup`, by the one rule both
`/me` and `/capabilities` read, so the two endpoints cannot disagree about it.
A new nullable `financial_profile.monthly_expense_minor_units` column carries
the second figure under the same money rules as income.

The wizard's own endpoints gate on the *prerequisites* only — phone, region,
consent — and never on `financial_setup` itself, because saving there is how
that step is cleared. `require_onboarded` gates everything else and is proven
on a throwaway route, as `require_feature` was.

The wizard's status machinery is removed entirely: the `status` field,
`POST /financial-setup/skip`, the `finished` flag and the two timestamp columns
behind them.

## Files changed

**Schema**

| File | Why |
|---|---|
| `alembic/versions/c8d41a6f3b92_monthly_expense_no_wizard_status.py` | Adds the expense column; drops `setup_completed_at` and `setup_skipped_at`. Reverses cleanly. |
| `app/models/setup.py` | The new column, the two dropped, and docstrings that no longer describe a wholly skippable wizard. |

**The rule and the gates**

| File | Why |
|---|---|
| `app/services/onboarding.py` | `financial_setup` joins the rule; `wizard_prerequisites` and `require_onboarded` are the two gates built on it. |
| `app/api/financial_setup.py` | Guard narrowed to prerequisites; the skip endpoint removed. |

**The wire**

| File | Why |
|---|---|
| `app/schemas/financial_setup.py` | `monthly_expense` in and out; `status` and `finished` gone. |
| `app/services/financial_setup.py` | Converts and stores the expense; `_status`, `skip_setup` and the `STATUS_*` constants removed. |

**Tests**

| File | Why |
|---|---|
| `tests/test_financial_setup.py` | The status class replaced by the expense, gate, reachability and `require_onboarded` classes. |
| `tests/test_onboarding_endpoint.py` | The one-rule walk now ends by saving both figures to reach `[]`. |
| `tests/test_me_endpoint.py`, `tests/test_capabilities_endpoint.py` | Step lists gain `financial_setup`. |

**Documentation**

| File | Why |
|---|---|
| `docs/tickets/M2.5-mandatory-financial-setup.md` | Records the no-status decision and scopes the mobile half to the shared contract. |
| `docs/tickets/M2.2-financial-setup-persistence.md` | Its supersession note now says the status machinery is gone, not "revisited". |
| `handoffs/ticket-29.md` | This report. |

## How to test

A throwaway Postgres, because a developer `.env` points at production:

```bash
docker run -d --name finai-pg-t29 -e POSTGRES_PASSWORD=postgres -p 55433:5432 \
  ghcr.io/pgmq/pg17-pgmq:v1.5.1@sha256:e6f893a793751ed30c89f5f88e95aa52b77c1a03440b7d118a996866489ac0c6
```

Then, with `DATABASE_URL` and `MIGRATION_DATABASE_URL` both pinned to
`postgresql://postgres:postgres@localhost:55433/postgres`:

1. `PG_CONTAINER=finai-pg-t29 PYTHON=.venv/bin/python scripts/check_migrations.sh`
   → *All migration checks passed*.
2. `SUPABASE_URL=https://example.supabase.co REQUIRE_DB=1 .venv/bin/python -m pytest -q`
   → **304 passed**.
3. `docker rm -f finai-pg-t29`.

The behaviour to read by hand is `TestTheWizardStaysReachable`: it is the one
that proves a user holding only `financial_setup` can still call the endpoint
that clears it.

## Acceptance criteria

| Criterion | Status |
|---|---|
| Missing either figure reports `financial_setup` from `/me` **and** `/capabilities`; supplying both clears it | **Met** — `TestFinancialSetupIsAnOnboardingStep`, and the `/me` vs `/capabilities` walk in `TestOneOnboardingRule`. |
| The wizard works while `financial_setup` is outstanding, and still refuses without phone, region or consent | **Met** — `TestTheWizardStaysReachable`, both halves. |
| A gated endpoint refuses while outstanding and passes once cleared | **Met** — `TestRequireOnboarded`, throwaway route. |
| `monthly_expense` round-trips (`"1800"` → `180000` → `"1800.00"`); a bad value names the field | **Met** — `TestMonthlyExpense`. |
| Status semantics decided and documented | **Met** — decided as *no status*; recorded in PRD §9, both ticket files and this report. |
| An old mobile build (unknown step) neither crashes nor passes the gate | **Met** — mobile side, `CapabilitiesDecodingTest`. |
| Migrations apply, reverse and re-apply; `pytest` passes; CI green | **Met locally**; CI is the check on the PR. |
| The apps cannot reach home while the step is outstanding; mandatory screens offer no Skip | **Deferred to 2.4** — see below. |

## Deviations / decisions

- **No status, rather than a fourth `in_progress` state.** The ticket
  recommended `in_progress` plus "a save clears the skip". That recommendation
  was stale — it restated a model already discarded earlier in the same
  conversation — and it was dropped on the manager's correction. A whole-wizard
  skip timestamp cannot name *which* optional field was declined, so it could
  not drive re-prompting; absence of a row is the record instead.
- **`finished` was removed too**, beyond the letter of the scope. Its only
  effect was setting `setup_completed_at`; with that column gone it was a
  no-op flag on the request body.
- **The step is reported even when earlier steps are also outstanding** — a new
  caller sees `["phone", "financial_setup"]`. The list is ordered and the client
  routes to the first, so this stays one rule rather than a rule plus an
  exception.
- **The mobile half is the shared contract only.** There is no navigation layer
  or wizard in that repo yet, so routing and the screens stay with 2.3 (#16) and
  2.4 (#17), which already carry the no-Skip requirement. Three acceptance
  criteria are marked deferred in the ticket rather than dropped.

## Open questions / follow-ups

- **Not deployed, and the migration has NOT been run against production.**
  Render is still suspended. `a3f7c2e91b84` (#25) is also still unapplied, so
  production is two migrations behind and both must be applied before the merged
  code serves traffic.
- **A save still replaces everything the wizard owns**, so a client that omits
  `income` clears it and re-raises the gate. That is the documented contract and
  2.4 is written to send the whole wizard every time, but it is sharper now that
  the field gates access than it was in #25.
- **The profile screens will need their own endpoints.** `save_setup` deletes
  every `investment` and `obligation` row for the household, so reusing it to
  edit optional data later would wipe what the profile added. Debts are already
  safe — only `entered_via_setup` rows are replaced. Worth settling when the
  profile ticket is drafted.
- **Per-field "declined" flags** remain out of scope, and nothing in this change
  forecloses them.
