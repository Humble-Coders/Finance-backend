# Handoff — ticket #25

**Ticket:** [#25 — \[M2\] Persist the financial setup wizard](https://github.com/Humble-Coders/Finance-backend/issues/25)
**Branch:** `ticket-25-financial-setup` · **Base:** `main` (`eaa9e3c`) · **Implementation:** `54bf10c` · **PR:** #28 · 13 files (10 implementation, 3 documentation)

## Summary

The setup wizard (PRD F1) now has somewhere to save: monthly income, debts, investments and monthly obligations, so M4 can seed a dashboard before a user has uploaded anything. `GET`, `PUT` and `POST /financial-setup(/skip)` read it, replace it, and mark it skipped.

A save **replaces what the wizard owns, in one transaction**, which is what makes it resumable: the client sends the whole wizard after every step, and sending it twice changes nothing. Debts are shared with M3's statement import, so only rows flagged `entered_via_setup` are replaced; a debt from a statement survives a wizard save.

Money crosses the boundary exactly once — decimal strings on the wire, integer minor units in Postgres, converted by `app/core/money.py` alone (PRD §4.4). Every amount is converted **before** anything is deleted, so a bad figure in the last row cannot leave a household with half a wizard, and the 422 names the field (`debts.0.balance`) so the client can highlight the row the user typed.

All three endpoints refuse with 409 until onboarding is complete: without a region there is no currency to denominate in (#24).

## Files changed

### Data model and migration
| File | Why |
|---|---|
| `app/models/setup.py` *(new)* | `FinancialProfile` (one per household: income — nullable, since every step is skippable — the currency those amounts are in, and the two timestamps status is **derived** from, so a status cannot disagree with them), `Obligation`, `Investment` |
| `app/models/planning.py` | `Debt.entered_via_setup` — the flag that lets the wizard replace its own debts without touching M3's |
| `app/models/__init__.py` | Exports |
| `alembic/versions/a3f7c2e91b84_financial_setup.py` *(new)* | The three tables with their indexes, currency checks and RLS; the debt flag added NOT NULL through a temporary default; downgrade reverses everything |

### API
| File | Why |
|---|---|
| `app/services/financial_setup.py` *(new)* | The logic: read, replace-in-one-transaction, skip; amount and rate conversion with a named field on failure; status derived from the timestamps |
| `app/api/financial_setup.py` *(new)* | The three endpoints, thin over that service, with the onboarding gate |
| `app/schemas/financial_setup.py` *(new)* | Request/response shapes; amounts are decimal strings both ways; lists capped at 20; names 1–255 |
| `app/services/capabilities.py` | `currency_for(session, household)` — the country pack's currency, or the documented default; reused rather than resolving a whole capabilities payload |
| `app/main.py` | Registers the router |

### Documentation
| File | Why |
|---|---|
| `docs/tickets/M2.2-financial-setup-persistence.md` | This ticket's own file, with a note that **2.5 supersedes its skip/status semantics** (manager decision, 2026-09-12: phone, region, monthly income and monthly expense become mandatory; debts, investments and itemised obligations stay optional) |
| `docs/tickets/M2.5-mandatory-financial-setup.md` | Ticket 2.5's file (#29) — the mandatory gate that follows this ticket. Saved here because it only makes sense once this exists; it changes no behaviour in this PR |
| `handoffs/ticket-25.md` | This report |

### Tests
| File | Why |
|---|---|
| `tests/test_financial_setup.py` *(new)* | 17 tests (24 cases with parametrisation), one or more per acceptance criterion, including that each list keeps the order it was sent in |

## How to test

Your `.env` points at **production**. The variables below redirect everything to a local container — never run the suite without them.

```bash
docker run -d --name finai-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 \
  ghcr.io/pgmq/pg17-pgmq:v1.5.1@sha256:e6f893a793751ed30c89f5f88e95aa52b77c1a03440b7d118a996866489ac0c6
export L=postgresql://postgres:postgres@localhost:55432/postgres
DATABASE_URL=$L MIGRATION_DATABASE_URL=$L SUPABASE_URL=http://localhost:54321 \
  PG_CONTAINER=finai-pg PYTHON=.venv/bin/python scripts/check_migrations.sh   # → All migration checks passed
DATABASE_URL=$L MIGRATION_DATABASE_URL=$L SUPABASE_URL=http://localhost:54321 \
  .venv/bin/alembic check                                                     # → No new upgrade operations detected
REQUIRE_DB=1 DATABASE_URL=$L MIGRATION_DATABASE_URL=$L SUPABASE_URL=http://localhost:54321 \
  .venv/bin/pytest -q                                                         # → 295 passed
.venv/bin/ruff check . && .venv/bin/ruff format --check .                     # → clean
docker rm -f finai-pg
```

## Acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| Amounts round-trip exactly (`"1200"` → `120000` → `"1200.00"`); no float in the path | ✅ Met | `test_saves_and_returns_every_part_exactly`, `test_stores_integer_minor_units` reads `balance_minor_units == 120000` straight from the database. Conversion happens only in `app/core/money.py`; `test_models.py` already fails the build on any floating-point column |
| Negative amounts, excess precision, non-numeric strings and oversized lists → 422 naming the field | ✅ Met | `TestValidation` — 8 parametrised cases asserting `detail.field` (`income`, `debts.0.balance`, `debts.0.interest_rate_percent`, `investments.0.amount`, `obligations.0.monthly_amount`), plus the 21-item list and an empty name |
| Two identical `PUT`s leave identical data; a `PUT` with fewer debts drops the rest | ✅ Met | `test_two_identical_saves_leave_identical_data`, `test_a_save_with_fewer_debts_drops_the_rest`; order is kept too — `TestOrdering` |
| A `PUT` never touches a debt with `entered_via_setup = false` | ✅ Met | `test_never_touches_a_debt_it_does_not_own` — a statement-style debt survives two wizard saves, including one that empties the list |
| `status`: `not_started` → `completed` with `finished: true`; → `skipped` with skip; data before a skip is kept | ✅ Met | `TestStatus` — four tests, including that a later save does not un-complete it |
| Before onboarding is complete, all three endpoints return 409 with `onboarding_required` | ✅ Met | `test_all_three_endpoints_refuse_until_onboarding_is_done` |
| A household can never read or write another household's setup | ✅ Met | `test_one_household_never_sees_another`; every endpoint resolves its own household through `current_identity` and nothing accepts a household id from the client |
| Migrations apply, reverse, re-apply in CI's `database` job; `pytest` passes; CI green | ✅ Met | CI on #28: run 34672574938, and again on the review fixes. Locally: `check_migrations.sh` and `alembic check` clean, 295 passed (259 on `main`), ruff clean |

## Deviations / decisions

1. **The response also returns `currency`** (manager-confirmed), so the wizard can render amounts without a second call to `/capabilities`.
2. **Investments and obligations are wholly wizard-owned**, so a save replaces all of them; only debts need the flag, because statements will create debts later.
3. **Status is derived from the two timestamps** rather than stored as its own column — it cannot then disagree with them. Completed outranks skipped, and a later save never un-completes.
4. **Negative amounts are refused** even though `app/core/money.py` supports them (refunds, debts): nothing the wizard asks for can be negative.
5. **Interest is sent as a percentage string** (`"5.25"`), capped at 100, stored as basis points — the integer form `debt.interest_rate_bps` already uses.
6. **`currency_for` was added to the capabilities service** rather than resolving a whole payload for one field; the resolver is untouched.
7. **The whole request is converted before anything is deleted**, so a rejected amount leaves the previous data intact.
8. **The models live in `app/models/setup.py`**, not in `planning.py` with budgets and goals.

## Review round — fixes applied

From the manager review of #28:

1. **The wizard's list order was not preserved.** `_payload` ordered by `created_at` then name, but every row written in one save shares that timestamp (Postgres `now()` is transaction time), so **name** decided the order: sending `Visa, Car loan, Student loan` returned `Car loan, Student loan, Visa`. The client saves after each step, so a user would have watched their rows reshuffle mid-wizard; the single-item tests could not catch it. Now a `position` column on `obligation`, `investment` and `debt` records the order sent and orders the reads. On `debt` it is nullable — statement-derived rows (M3) have no wizard position. The migration was **amended rather than stacked**: it has not reached production, which is still at `5b2e9f7c1d34`.
2. **Two concurrent saves could duplicate rows, or 500.** The save deleted and re-inserted with no lock, so overlapping saves could each delete what they saw and both insert; on a household's first save, both would insert a `financial_profile` and the loser would hit the unique constraint as a 500. `save_setup` and `skip_setup` now take a row lock on the household first, so they queue.
3. **Nits.** The `exponent_for(currency)` guard only rejected malformed codes and would have surfaced as a 500 — removed. One `Field(...)` instance was shared by three schema models, which pydantic v2 discourages — each field now declares its own.

**Left open for the Product Owner:** resuming after a skip still reports `skipped` — nothing clears `setup_skipped_at`, so someone who skipped and came back could be routed past a wizard they are actively filling in. Changing it is a product call, not a defect to fix silently.

## Open questions / follow-ups

- **Not deployed.** Render has both services suspended, so this cannot be verified against the live API; it needs no Render to build, test or merge. `finai-worker` stays suspended deliberately until M3.
- **A region change after saving** does not reinterpret stored amounts: `financial_profile.currency` records what they were denominated in (PRD §4.6 — historical records keep their currency). M4 should read that column rather than assume the household's current currency.
- **Nothing reads these figures yet.** The budget generator and health score (M4) are their first consumer, and `FinAI-Mobile-2026#17` is the UI.
- **Ordering is a stored column.** Each list keeps the order it was sent in, via `position` — see the review round below for why `created_at` could not do that job.
