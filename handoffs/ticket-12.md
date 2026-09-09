# Handoff — ticket #12

**Ticket:** [#12 — \[M1\] Back /capabilities with the database](https://github.com/Humble-Coders/Finance-backend/issues/12)
**Branch:** `ticket-12-capabilities-from-database` · **Base:** `main` · 6 files, +661 / −50

## Summary

`/capabilities` now resolves from the database instead of hardcoded values. The payload shape was already final — both mobile clients build against it — so only the source of the data changed, plus one addition the stub lacked entirely: `require_feature`, the enforcement half of the shown-versus-allowed rule.

Country packs, feature availability and plan entitlements all compose in **one resolver**, per PRD §4.6. Adding a market is now an `INSERT`, proven by a test that inserts a second country pack and resolves against it with no code change.

197 tests pass; **123 need no database** and run in ~0.5s.

## Files changed

| File | Why |
|---|---|
| `app/services/capabilities.py` *(new)* | The resolver and `require_feature`, out of the route so both are testable without HTTP |
| `app/schemas/capabilities.py` *(new)* | Response models moved out of the route, matching `schemas/identity.py` |
| `app/api/capabilities.py` | Rewritten thin: `current_household` → `resolve` → response |
| `alembic/versions/b56e4dda349a_…` *(new)* | Seeds the six feature rows the clients know about |
| `tests/test_capabilities.py`, `tests/test_capabilities_endpoint.py` *(new)* | 22 tests: precedence, plan gating, unknown region, enforcement, new-market-is-data-only |

## How to test

```bash
git checkout ticket-12-capabilities-from-database
source .venv/bin/activate && alembic upgrade head && pytest -q
```

Expect **197 passed** with `.env`; **123 passed, 74 skipped** without one.

By hand:

1. `GET /me` then `GET /capabilities` as a new user → `region: null`, `onboarding_required: ["phone"]`, and `document_upload` **enabled**
2. `bank_linking` is `enabled: false` with `reason: "coming_soon"` — off, and saying why
3. Insert a `country_pack` row for a new country and resolve — no code change, no deploy

## Acceptance criteria

| Criterion | Status |
|---|---|
| One resolver composing plan + region + rollout | ✅ Met — `resolve()`; grep finds no second place composing them |
| `GET /capabilities` returns the caller's household payload | ✅ Met — via `current_household` |
| Unknown-region fallback, never guessing, never 500 | ✅ Met — documented defaults, `region: null` preserved |
| `onboarding_required` when the region is unknown | ✅ Met |
| `require_feature` → 403 disabled, pass-through enabled | ✅ Met — and **fails closed**: an unknown key is refused, not allowed |
| Country-specific content from the pack, never Python constants | ✅ Met |
| Adding a country needs rows only | ✅ Met — `TestAddingACountryIsDataOnly` inserts GB and resolves |
| No `if country == "CA"` anywhere | ✅ Met |
| `pytest` passes; CI green | ✅ 197 pass locally; CI on the PR |

## Deviations / decisions

**1. Precedence is most-specific-wins** — country+plan > country > plan > global. Country outranks plan when only one is set: region availability is usually a legal or operational constraint, plan is commercial, so a paid plan must not unlock something a country does not offer. Encoded in `TestPrecedence` so it is a recorded decision rather than an accident of query order.

**2. Feature rows are seeded by a migration.** Without them the payload would go silently empty when the hardcoded stub was removed — and a client cannot distinguish "feature absent" from "feature off", so absence is not an acceptable way to say no.

**3. `document_upload` ships enabled**; everything else is off with `coming_soon`. Upload is the core loop of v1.

**4. An unknown region does NOT force features off.** The first version did, and it **disabled `document_upload` for every user** — a household's region is NULL until the phone step completes, so blanket region gating turned off v1's core loop for everyone. A test caught it before review.

The override was also redundant: a market-specific feature is globally off with a country row enabling it, so no matching pack already produces the right answer through precedence. Removing it was both the fix and a simplification.

**5. `require_feature` fails closed.** An unknown feature key returns 403 rather than passing through, so a typo cannot open an endpoint.

## Open questions / follow-ups

- **Nothing calls `require_feature` in production code yet** — it is proven by tests on a throwaway route. The first genuinely gated endpoint (bank linking, M3 upload limits) should adopt it rather than reinvent gating.
- **Entitlements are read but never written.** Every household resolves to `free`, because nothing creates a `subscription_entitlement` row. That is M7's job; until then the paid path exists only in tests.
- **`disclaimer_version` is returned but no `disclaimer_version` row is seeded.** The pack points at `ca-v1`, which does not exist in that table yet — harmless today since the client only echoes the string, but the content needs writing before launch (Appendix A).
- **The suite now takes ~15 minutes** against a cross-region database, against ~0.5s for the 123 that need none. **#15** must keep them separate jobs.
- Small correction: the commit message says "129 need no database"; the true figure is **123**.

## Review round — fixes applied

Three findings from `/manager-review 19`, two of them blocking. Both blockers were things the code *claimed* were handled.

**1. `uq_feature_scope` did not prevent equal-specificity ties — and a comment said it did.**

`NULL` means "any" in this table, and Postgres treats NULLs as distinct, so the three-column unique index only ever constrained the one scope where both columns are set:

```
global      (NULL, NULL): DUPLICATE ACCEPTED
country only(CA,   NULL): DUPLICATE ACCEPTED
plan only   (NULL, free): DUPLICATE ACCEPTED
country+plan(CA,   free): rejected
```

That includes every row this ticket seeds. With no `ORDER BY`, the winner was whichever row Postgres returned first — so the same two rows resolved differently once one moved:

```
resolve() before touch: enabled=True
resolve() after  touch: enabled=False
```

A feature could flip on or off after a vacuum. Same trap as the duplicate system-category slugs fixed in `1daf2b084378`.

Fixed in `c3a91e7d4b28` with a partial unique index per scope, so the NULLs sit in the predicate where they can be compared instead of in the indexed columns where they cannot. A `COALESCE` expression index would have covered all four in one, but casting the `plan` enum to text is only `STABLE`, so Postgres refuses it in an index expression. `_feature_rows` also gained a deterministic `ORDER BY`, so even a tie that somehow escaped the index resolves the same way every time. The migration refuses to run if the table already holds a duplicate, naming the feature and scope rather than failing with a bare index error.

**2. `is_launched` was never read — a staged market was served as fully launched.**

```
pack row has is_launched = FALSE
  region: DE   currency: EUR   locale: de-DE
  content: {'tax_accounts': ['Riester'], 'disclaimer_version': 'de-v1'}
```

The comment on the `pack is None` branch claimed it covered "known but unlaunched", but an unlaunched market *has* a pack row and never reached that branch. The test that looked like coverage — `test_an_unlaunched_country_keeps_its_code_but_has_no_content` — used `"ZZ"`, a country with no pack row at all, which is a different case.

`disclaimer_version` is why this matters: it points at legal copy that has not been approved for that market, and shipping unapproved disclaimer text is exactly what the flag guards (Appendix A). The pack query now filters on `is_launched`, the mislabelled test was renamed to `..._unconfigured_country_...`, and `TestUnlaunchedMarket` covers the real case — staged serves no content, and flipping the one column opens the market.

**3. An unknown feature key reported `reason: "region_unsupported"`.** It is a missing row in our own seed data, not a fact about the caller — the old reason sent whoever debugged it looking at country packs. Now `unknown_feature`. Still 403; only the explanation changed.

### Not fixed — raised as follow-ups

- **`onboarding_required` keys off `country_code`, not a verified phone**, though the ticket's criterion is phrased in terms of the phone. They coincide only because nothing else sets the region today. Region-from-timezone has been discussed; the moment it lands, `country_code` goes non-NULL without a phone and this list silently empties while the phone step is still outstanding. Needs either a direct phone check or the coupling written down as a deliberate invariant — worth doing in **2.1**, which owns region resolution.
- **`_plan_for` ignores `expires_at`**, while the model docstring says history is kept "by leaving expired rows in place rather than updating them". If nothing flips `is_active`, an expired entitlement keeps granting its plan. M7 owns billing; the two should be reconciled there rather than left to a webhook nobody has written.
- `_mount` in `test_capabilities_endpoint.py` adds routes to the global `app` and never removes them; the table grows for the life of the session. Harmless now, untidy later.
