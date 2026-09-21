# Handoff — ticket #34

**Ticket:** [#34](https://github.com/Humble-Coders/Finance-backend/issues/34) — [M3] Turn redacted statement text into transaction rows

## Summary

`POST /statements/parse` takes the redacted text an app extracted on the device
and returns transaction rows. It persists nothing but the import record — 3.3
saves the rows once the user has confirmed them, because a model is not
authority over someone's financial records.

Around that sit the parts that make it safe to run: every amount a row claims is
checked against the submitted text verbatim, the refusals run cheapest-first so
a disallowed request cannot reach a paid provider, and 422s no longer echo the
statement back. `document_upload` became `statement_import`, losing the five
columns that described a file we never receive. Consent to AI processing is its
own policy, version and consent event.

Two rounds of review found nine further defects, all fixed and pinned by tests;
they are listed under *Deviations & decisions* because several changed what the
ticket asked for.

## Files changed

**The endpoint and its gates**
- `app/api/statements.py` — the parse endpoint: feature gate → consent → quota →
  size → model, in that order, so nothing free runs after something expensive.
- `app/schemas/statements.py` — request and response shapes. No `max_length` on
  `text`: Pydantic would answer 422 and quote the statement; the endpoint answers
  413 instead.
- `app/api/errors.py` — the validation handler, extracted from `app/main.py` so
  testing it does not require building the app (and therefore settings).

**Reading the statement**
- `app/services/statements.py` — windowing, the prompt, row validation, the
  verbatim-amount check and the overlap merge. **The statement period arrives as
  a field** (`statement_period_start`/`_end`) and is put on every window:
  statements print the year once, in the header block the on-device redactor
  deliberately drops, and without it the prompt's own rule drops every row it
  cannot date. Scraping the header is kept only as a fallback.
- `app/services/llm.py` — provider behind a protocol, chosen by settings, with
  one HTTP connection per parse.

**Consent**
- `app/services/ai_consent.py` — the AI-processing policy in force, and whether
  this user agreed to *that version*.
- `app/api/legal.py`, `app/schemas/legal.py` — read the policy, record consent.

**Schema**
- `alembic/versions/a1c4e7b90f22_*` — rename, drop the file columns, add
  `source_kind`/`page_count`, create `statement_import_text`, add the enum value.
- `alembic/versions/b2d5f8a13c47_*` — seed the policy. Separate because Postgres
  refuses to use an enum value in the transaction that added it.
- `app/models/money.py`, `app/models/enums.py`, `app/config.py`.

**Tests** — `test_statement_parsing.py` (guards, windowing, merge),
`test_statements_endpoint.py` (gates, quota, retention, failures),
`test_llm_client.py` (provider selection), `test_validation_does_not_echo.py`.

## How to test

```bash
gh pr checkout 40
DATABASE_URL="" MIGRATION_DATABASE_URL="" .venv/bin/python -m pytest -q   # 216 / 160 skipped
```

Database-backed tests need a throwaway Postgres and run in CI's `database` job —
**do not point them at the configured database, which is production.**

Behaviour worth seeing directly:

```bash
.venv/bin/python -c "
from app.services import statements as s
t = '\n'.join(['x'*3000]*60)          # what on-device OCR looks like
w = s._windows(t)
print(len(w), 'windows,', round(sum(map(len,w))/len(t),2), 'x the statement sent')"
```

## Acceptance criteria

| Criterion | Status |
|---|---|
| Rows match the fixture; decimal strings, no float | **Met**, though the fixture is three synthetic lines, not a real layout |
| An amount not in the text is dropped and counted | **Met** |
| A chunked statement returns each transaction once | **Met** |
| 409 `consent_required`, then success once consented | **Met** |
| Second import in a month → 429 | **Met** |
| Feature off → 403 before any model call | **Met**, asserted via `model.calls == 0` |
| Model failure → 502, row `failed`, no rows | **Met** |
| No statement content in logs; 422 does not echo | **Met** |
| One household cannot read another's imports | **Not met** — no read endpoint exists yet; belongs to 3.3/3.4 |
| A successful import stores no text | **Met**, asserted directly |
| Text stored only on opt-in *and* a bad import | **Met** |
| Retained text in export; deleted with the account | **Not met** — no export exists. The FK cascade covers deletion incidentally |
| Provider swap by settings | **Met** |
| Synthetic fixtures only | **Met** |
| CI green | **Met** — 216/160 fast, 160 database, migrations apply/reverse/re-apply |

## Deviations & decisions

- **Size limit is 200k characters, not 1 MB.** A megabyte was an unbounded number
  of sequential model calls — over an hour in one request. Ticket updated.
- **A failed import does not consume the monthly quota**, and a parse that finds
  no rows is recorded as failed. Otherwise the 502's "please try again" is false:
  on the free tier's single import, the retry returns 429.
- **The consent code is `consent_required`**, matching tickets 3.1 and 3.6.
- **The capability key stays `document_upload`** though the table was renamed —
  the apps already read it.
- **No filename column.** `statement-jane-smith.pdf` is personal data with no use.
- **Retention is weaker than a flat 30 days**: the purge runs on the import path,
  so expired text survives until the next import. The correction to the PRD and
  Appendix A.5 is committed but **still open** in
  [FinAI-Mobile-2026#34](https://github.com/Humble-Coders/FinAI-Mobile-2026/pull/34)
  — until it merges, the PRD on `main` promises a flat 30-day expiry the code
  does not provide, so **that PR blocks this one**. A scheduled sweep is a
  before-launch item either way.

## Open questions / follow-ups

- **The fixture should become a realistic multi-page Canadian statement.** What
  exists proves the guards, not the parsing.
- **The device must send the statement period**, read *before* it redacts —
  [FinAI-Mobile-2026#29](https://github.com/Humble-Coders/FinAI-Mobile-2026/issues/29)
  and [#31](https://github.com/Humble-Coders/FinAI-Mobile-2026/issues/31) were
  updated for it. Its redactor drops the block above the first transaction on
  page 1, which is where the name, the address *and the year* live. Without the
  field, a statement that reads perfectly returns nothing.
- **The client must handle two 413s** the mobile ticket predates: `statement_too_long`
  (over 200k characters) and `too_many_transactions`. Ticket
  [FinAI-Mobile-2026#31](https://github.com/Humble-Coders/FinAI-Mobile-2026/issues/31)
  has been updated; it previously listed only 403, 409, 429 and 502, so 3.6 would
  have rendered both through a generic "something went wrong".
- **A statement over 2,000 transactions is refused** with 413 `too_many_transactions`
  rather than imported in parts. Splitting one across imports would need dedup
  across them, which is 3.3's problem, not this endpoint's.
- **A credit written `12.40-` or `(12.40)`** — both real Canadian conventions —
  fails the verbatim-amount check against the `-12.40` the model returns, so it
  is rejected and counted rather than saved with a guessed sign. Losing a row
  the user can see in the review queue beats inventing a figure.
- **`_merge` cannot separate two identical transactions that land in different
  windows** with no overlap between them. Narrow — identical same-day rows are
  normally adjacent — but real, and documented in the function.
- **Export and cross-household read coverage** move to 3.3/3.4 with the endpoints
  they need.
- **Dates could be checked against the period** now that we have one — a row
  outside it by more than a posting delay is suspicious. Deliberately not added
  late in review; it belongs with 3.3's confidence rules.
- **The prompt is untuned against real bank layouts.** Review-queue volume (3.4)
  is the signal for that, and the opt-in diagnostic text is how we see why.
