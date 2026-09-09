# FinAI Backend — conventions

## What this is

The FastAPI service behind FinAI, an AI money-coach platform. It owns **all** business
logic: transaction extraction, categorization, budgets, goals, debt math, the health
score, and the AI chatbot. Mobile (Kotlin Multiplatform) and web (React) clients are
display layers. Product spec: **https://github.com/Humble-Coders/FinAI-Mobile-2026/blob/main/docs/PRD.md** — the mobile repo holds the canonical PRD; this repo deliberately keeps no copy, so there is one decision log and it cannot drift.

**Positioning constraint:** this is *educational guidance*, **not regulated financial
advice**. The AI must never give individualized securities recommendations. Projections
carry disclaimers.

## Stack

Python 3.11+ · FastAPI · SQLAlchemy 2 (async) · Alembic · Supabase Postgres · Render.

## Hard rules

### Money
- **Postgres stores integer minor units. The API transports decimal strings.**
- `app/core/money.py` is the **only** place conversion happens. Nothing else multiplies
  or divides an amount to change representation.
- **Never `float`.** `Decimal` throughout.
- Excess precision raises rather than silently rounding; callers opt into rounding
  explicitly. Losing a fraction of a cent quietly is how money bugs start.
- Negative amounts are legitimate (refunds, debts, overruns).

### Financial math
- All authoritative figures — health score, budget allocations, goal projections, debt
  schedules, spending aggregates — are computed **here**, in deterministic, unit-tested
  code. Clients never recompute them.
- **The LLM never produces a number.** Figures come from the database or the math
  engine; the LLM explains them. The chatbot answers by tool-calling over SQL, never
  from model recall.

### Auth & access

- **`current_household` performs a write.** It resolves the caller's token to a
  user and household, **creating them on first call**, and commits — so every
  endpoint depending on it writes before its body runs, including GETs. This is
  deliberate (it saves clients a bootstrap round trip) but surprising, so do not
  assume a read-only handler is read-only.
- **Never look a user up by `auth_user_id`.** It records only the *first*
  Supabase account seen for a person; when a second provider is linked by phone,
  that `sub` lives in `user_identity`. Resolution goes through `user_identity`,
  or it silently fails for anyone using more than one provider.
- Clients authenticate against **Supabase Auth**; this service only verifies the JWT
  (`app/auth.py`). No custom auth.
- The `service_role` key exists only in this service's environment. It must never reach
  a client bundle.
- Row Level Security stays enabled in Postgres as defence in depth, even though this
  service is the only database caller.

### Region & entitlements
- One resolver composes plan + region + rollout flags (`app/api/capabilities.py`).
  Not three parallel systems.
- The capabilities payload controls what clients **show**; every gated endpoint must
  independently re-check what is **allowed**. A hidden feature is not a secured feature.
- Country-specific data (tax accounts, disclaimers, pricing) lives in database country
  packs — adding a market is a data operation, not a deploy.

### Async work
- Anything long-running, retry-heavy, or LLM-dependent belongs in the **worker**, not a
  request handler. Uploads return `status: queued` immediately.
- Extraction must be resumable and idempotent — a partially imported statement in
  someone's financial records is worse than a failed import.

### Privacy (see PRD Appendix A)
- **No document bytes, raw statement text, or unredacted account numbers in logs, error
  traces, or crash reports.** We tell users documents are deleted; copies in logs are
  the violation.
- Redaction happens **before** any LLM call, never after. Categorization receives only
  `(merchant_string, amount)`.
- Source documents are deleted on user confirmation or after 72 hours, whichever is
  first — enforced by a scheduled job, not manually.
- LLM calls go through business **API tier** accounts with no-training terms only.

### Database
- **UUID primary keys** — no global sequences (multi-region readiness, PRD §4.2).
- Every financial record is **household-scoped**; `household.country_code` is the
  routing key.
- Dedup is enforced by database constraints, not application logic alone.
- Migrations via Alembic; never hand-edit the database.
- `DATABASE_URL` is the Supabase **transaction pooler** (6543). asyncpg prepared
  statement caching must stay disabled — see `app/db.py`.

### DON'T
- No `float` for money; no conversion outside `app/core/money.py`.
- No LLM-generated figures presented as data.
- No secrets in the repo (it is **public**).
- No PII in logs or analytics.
- No blocking a request on work that belongs in the worker.
- No per-country branches in code — use country packs.

## Testing

- `app/core/money.py` and every math engine get thorough unit tests including zero,
  negatives, and precision edges. These are the highest-value tests here.
- `/healthz` must not touch the database (a DB blip would cycle the service);
  `/readyz` may.

## Workflow

`docs/PROCESS.md` (created by `/humble-task-force:setup-tickets`).

| Command | When |
|---|---|
| `/humble-task-force:draft-ticket <thing>` | Manager drafts from the PRD |
| `/humble-task-force:start-ticket` | Developer picks it up |
| `/humble-task-force:handoff` | Handoff report from the real diff |
| `/humble-task-force:manager-review` | Review PR against acceptance criteria |
