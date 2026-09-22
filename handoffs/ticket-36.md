# Handoff — ticket #36

**Ticket:** [#36](https://github.com/Humble-Coders/Finance-backend/issues/36) — [M3] Save extracted transactions: accounts, dedup and categories

## Summary

Turns the rows #34 parses into data the product can use. An import is saved in
one transaction, deduplicated against what is already there, and categorized on
merchant and amount alone.

The ticket asked for two things a four-column dedup key cannot both do: *the
same statement twice saves once* and *a genuine repeat purchase is saved*. Two
$5.25 coffees at one shop on one day are byte-identical to one coffee imported
twice — only context separates them. So the key gained a fifth column,
`occurrence`, numbering each row within its own import. A re-import produces the
same numbers and collides exactly; a genuine third coffee in a later statement
does not. This works because a statement is cumulative: any statement covering a
day lists every transaction of that day.

Accounts got the minimum an import needs — create and list — because the table
had never been written to and an import had nowhere to land.

## Files changed

**New**
- `app/services/normalization.py` — the comparison key and the readable merchant
- `app/services/ledger.py` — occurrence numbering, exact and near dedup
- `app/services/categorization.py` — slugs, corrections, degradation
- `app/api/accounts.py`, `app/schemas/accounts.py`
- `alembic/versions/c7e1a93b4d82_*` — `occurrence`, `review_reason`,
  `duplicate_of_id`, unique account name per household
- `tests/test_normalization.py`, `tests/test_ledger_endpoint.py`

**Changed**
- `app/api/statements.py` — confirm and read endpoints
- `app/models/money.py`, `app/models/enums.py`, `app/schemas/statements.py`
- `tests/test_models.py` — the dedup key test, which correctly failed

## How to test

```bash
DATABASE_URL="" MIGRATION_DATABASE_URL="" .venv/bin/python -m pytest -q
```

For the database suite, against a throwaway Postgres — **never the configured
database, which is production**:

```bash
colima start                      # or Docker Desktop, if it works for you
docker run -d --name finai-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 \
  ghcr.io/pgmq/pg17-pgmq:v1.5.1@sha256:e6f893a793751ed30c89f5f88e95aa52b77c1a03440b7d118a996866489ac0c6
PG="postgresql+asyncpg://postgres:postgres@localhost:55432/postgres"
DATABASE_URL=$PG MIGRATION_DATABASE_URL=$PG SUPABASE_URL=https://local.test \
  .venv/bin/python -m alembic upgrade head
DATABASE_URL=$PG MIGRATION_DATABASE_URL=$PG SUPABASE_URL=https://local.test \
  .venv/bin/python -m pytest -q -m integration
```

## Acceptance criteria

| Criterion | Status |
|---|---|
| Same statement twice saves once | Met |
| Overlapping statements save the union | Met |
| Genuine same-day repeat survives | Met |
| Re-import after a better parse flags, not doubles | Met |
| Merge keeps the user's version | **Partial** — `may_merge_onto()` exists; the merge is 3.4's |
| Normalizer is deterministic | Met — 25 table-driven cases |
| Backfill guard | Met — every stored key equals today's output |
| Amounts round-trip, no float | Met — and a *bad* amount is 422 naming the row, not a 500 |
| Only merchant and amount reach the model | Met — asserted on the wire |
| Unknown slug → `other` + review, no category created | Met |
| Corrections never cross households | Met |
| Duplicate account name → 409 | Met |
| Cross-household isolation | Met |
| Migrations apply/reverse/re-apply; CI green | Met |

## Deviations & decisions

- **A fifth dedup column.** Explained above. The existing test pinning the
  four-column key failed, correctly — it was pinning a decision, and the
  decision changed.
- **Near matches are found on account, amount and date alone**, never on
  description similarity: the case they exist for is a re-import where the
  description is the one thing guaranteed to have changed.
- **Same-day window** (manager decision, 2026-09-22), narrowed from ±3 days.
  Accepted cost recorded beside the constant: a re-import that shifts a date
  lands twice.
- **Rows from the same import never flag each other.** A statement listing two
  lines is a statement saying both happened.
- **Possible duplicates are saved and flagged**, not held back (manager
  decision): nothing is ever missing from the ledger.
- **`confirmed_at` is set only when nothing is outstanding**, which is what 3.4
  reads it to mean.
- **Amounts on this endpoint are what a person typed**, not what we parsed —
  the review screen lets them correct one. So `12,40`, `12.345` and an empty
  field are ordinary inputs, answered with 422 naming `rows.N.amount` rather
  than an unhandled MoneyError that took the whole import with it.
- **A row with no merchant never reaches the model** — there is nothing to
  categorize with, and a guess that looks confident is worse than a flag.

## Open questions / follow-ups

- **Inserts are still one round trip per row.** The near-match is one query for
  the whole import now; the inserts are not. Measured locally: 50 rows 0.17s,
  200 rows 0.47s, 500 rows 1.11s — fine for the statements we have seen (the
  fixture is 25 rows), and worth bulk-inserting if year-end statements become
  common.
- **`may_merge_onto()` has no caller.** 3.4 owns the merge; the predicate is
  here so the rule lives beside the data it protects.
- **Manual entry (3.5) must pick an occurrence.** Always claiming 1 would
  collide with an imported row for the same purchase — which is correct when it
  *is* the same purchase and wrong when it is a second one.
- **The categorization prompt is untuned.** The fixture's 25 merchants are the
  only real input it has seen.
