# Handoff — ticket #24

**Ticket:** [#24 — \[M2\] Resolve the household's region from the verified phone](https://github.com/Humble-Coders/Finance-backend/issues/24)
**Branch:** `ticket-24-region-from-phone` · **Base:** `main` (`3ad842a`) · **Implementation:** `3b68f32` · 23 files

## Summary

A household's region is now derived **server-side from the verified phone** with libphonenumber — for a phone sign-up on its first authenticated call, for a Google/Apple user on the first call after the phone step. It is never guessed (a number that cannot be placed stays `NULL` and the user is asked), and a later phone change never moves it; the user changes it with `PUT /me/region`, and every change, derived or chosen, is audited in `household_region_change`.

`/me` and `/capabilities` now read **one onboarding rule** — `phone`, then `region`, then `consent` — which ends the disagreement seen live in Humble-Coders/FinAI-Mobile-2026#6. **Signup consent** is recorded against a specific terms version (`consent_event`, `POST /me/consent`, `GET /legal/terms`). Migration `5b2e9f7c1d34` adds the tables, a `kind` column on `disclaimer_version`, and DRAFT seed rows that back the CA `ca-v1` disclaimer and `terms-v1`.

Tests go from 208 to **255**, all passing against the CI Postgres image locally; the migration round-trips cleanly.

## Files changed

### Region
| File | Why |
|---|---|
| `app/services/region.py` *(new)* | `region_for_phone` — the `+` optional, libphonenumber parse, ISO alpha-2 or `None`, never a guess (non-geographic `+800` → `None`); `normalize_region`; `KNOWN_REGIONS` from libphonenumber's 245 region codes |
| `app/services/identity.py` | Every resolution path now ends in `_resolved` → `_derive_region`: a conditional `UPDATE` that applies only while the region is `NULL`, with the audit row written only when it changed a row |
| `requirements.txt` | `phonenumbers==9.0.39` |

### Onboarding and consent
| File | Why |
|---|---|
| `app/services/onboarding.py` *(new)* | The one rule (`phone` → `region` → `consent`); `current_terms` (newest effective global `account_terms` row); `has_accepted` |
| `app/api/me.py` | `/me` reads the rule and returns `terms {version, accepted}`; `PUT /me/region`; `POST /me/consent` |
| `app/api/capabilities.py` | Depends on `current_identity` so it can read the same rule, and sets `onboarding_required` from it |
| `app/services/capabilities.py` | The resolver no longer computes onboarding (`ONBOARDING_PHONE` and its NULL-region list removed) |
| `app/api/legal.py`, `app/schemas/legal.py` *(new)* | `GET /legal/terms` — the terms in force, for the consent screen; `current_user` only, so reading creates nothing |
| `app/schemas/identity.py` | `TermsStatus`, `MeOut.terms`, `RegionIn`, `ConsentIn` |
| `app/main.py` | Registers the legal router |

### Data model and migration
| File | Why |
|---|---|
| `app/models/identity.py` | `HouseholdRegionChange` (household-scoped; `source` phone/user; `changed_by_user_id` SET NULL) and `ConsentEvent` (user CASCADE; `disclaimer_version` RESTRICT) |
| `app/models/platform.py` | `DisclaimerVersion.kind` (`account_terms` / `regional_disclaimer`) |
| `app/models/enums.py`, `app/models/__init__.py` | `RegionSource`, `PolicyKind`; exports |
| `alembic/versions/5b2e9f7c1d34_region_resolution_and_consent.py` *(new)* | Both tables with indexes and RLS; `kind` added NOT NULL through a temporary default; two DRAFT seed rows with deterministic ids, `ON CONFLICT DO NOTHING`; downgrade reverses everything, enum types included |

### Tests
| File | Why |
|---|---|
| `tests/test_region.py` *(new)* | 25 unit tests: the `+1` matrix (CA, US, JM, PR) plus GB, IN, AU; the `+` optional; the Supabase test number; never-guess cases; `normalize_region` |
| `tests/test_onboarding_endpoint.py` *(new)* | 16 integration tests, one or more per acceptance criterion |
| `tests/test_identity_resolution.py` | A pre-#24 test asserted the region stays `NULL` for a phone sign-in; split into "the region comes from the phone" and "without a phone it is never guessed" |
| `tests/test_me_endpoint.py`, `tests/test_capabilities_endpoint.py` | Onboarding assertions now include `consent`; a phone user's capabilities now carry `region: "CA"` |
| `tests/test_capabilities.py` | Removed the resolver's onboarding test — the rule moved out of the resolver |
| `tests/test_models.py` | `consent_event` listed as scoped through `user`, like `user_phone_change` |

## How to test

Your `.env` points at **production**. Every command below overrides it with a local container — never run `pytest` without those variables.

```bash
uv pip install --python .venv/bin/python -r requirements-dev.txt   # the venv is uv-managed; it has no pip
docker run -d --name finai-pg -e POSTGRES_PASSWORD=postgres -p 55432:5432 \
  ghcr.io/pgmq/pg17-pgmq:v1.5.1@sha256:e6f893a793751ed30c89f5f88e95aa52b77c1a03440b7d118a996866489ac0c6
export L=postgresql://postgres:postgres@localhost:55432/postgres
DATABASE_URL=$L MIGRATION_DATABASE_URL=$L SUPABASE_URL=http://localhost:54321 \
  PG_CONTAINER=finai-pg PYTHON=.venv/bin/python scripts/check_migrations.sh   # → "All migration checks passed"
REQUIRE_DB=1 DATABASE_URL=$L MIGRATION_DATABASE_URL=$L SUPABASE_URL=http://localhost:54321 \
  .venv/bin/pytest -q                                                          # → 255 passed
.venv/bin/ruff check . && .venv/bin/ruff format --check .                      # → clean
docker rm -f finai-pg
```

## Acceptance criteria

| Criterion | Status | Evidence |
|---|---|---|
| `region_for_phone` table-driven test: `+1 416` → CA, `+1 212` → US, `+1 876` → JM, `+1 787` → PR, `+44` → GB, `+91` → IN, `+61` → AU, garbage/empty → None, and the Supabase test number recorded | ✅ Met | `tests/test_region.py`. The test number `14165550100` (fictional 555-01xx) resolves to **CA**, so test sign-ups get Canada rather than the region step |
| A phone-OTP household gets its region on the first call; a Google/Apple one on the first call after the phone is verified | ✅ Met | `test_a_phone_user_gets_their_region_on_the_first_call` (using the real, `+`-less claim), `test_a_google_user_gets_it_once_the_phone_is_verified`, `test_the_region_comes_from_the_phone` |
| An existing region is not changed by a later phone change | ✅ Met | `test_a_later_phone_change_never_moves_the_region` — phone moves to `+44`, region stays CA, one audit row |
| `PUT /me/region` changes the region, audits with `source = user`, rejects a non-ISO code with 422 | ✅ Met | `TestRegionOverride` — `gb` → GB audited; `ZZ`, `CAN`, `001`, `""` → 422 `unknown_region`; an unchanged region writes nothing; an unlaunched country (DE) is accepted |
| Every region change has a `household_region_change` row | ✅ Met | The derived change (`source = phone`, acting user recorded) and the override are both asserted; repeat calls log once |
| `/me` and `/capabilities` return the same `onboarding_required` in every state | ✅ Met | `test_me_and_capabilities_agree_in_every_state` — no phone, phone without region, consent pending, complete |
| A number that cannot be placed leaves the region NULL, reports `["region"]`, and the override completes onboarding | ✅ Met | `test_a_number_that_cannot_be_placed_asks_for_a_region` (`+800` freephone) — `["region", "consent"]`, then `["consent"]` after the override |
| Consent recorded with its policy version, append-only, and `consent` outstanding until accepted | ✅ Met — *append-only in code* | `TestConsent`: recorded against `terms-v1`; a version not in force → 409 `terms_version_mismatch`; accepting twice records once. No update/delete path exists; not database-enforced (Deviation 3) |
| A `disclaimer_version` row backs `ca-v1` | ✅ Met | `test_the_ca_pack_disclaimer_is_backed` |
| Migrations apply, reverse, re-apply in CI's `database` job; `pytest` passes; CI green | ⚠️ Met locally — CI pending the PR | `check_migrations.sh` passes against the CI image locally; 255 passed; ruff clean. CI runs when the PR opens |
| No endpoint reads or writes another household's data | ✅ Met — by construction | The new endpoints act only on `identity.user` / `identity.household` from `current_identity`; nothing takes a household id from the client. No dedicated cross-household test was added |

## Deviations / decisions

1. **Consent references the terms row instead of copying its kind and version.** The ticket listed `kind` and `policy_version` on `consent_event`; the event instead holds `disclaimer_version_id`, and `kind` lives on `disclaimer_version` (new column). Same facts, and the agreed text stays retrievable — what that table exists for.
2. **`GET /legal/terms` added** (not in the ticket) so the consent screen in Humble-Coders/FinAI-Mobile-2026#16 has text to show. Agreed at planning.
3. **Append-only is enforced in code, not by a database trigger.** Account deletion (PRD Appendix A.5) must be able to remove a user's rows, which the `user` CASCADE does; a trigger blocking deletes would prevent it.
4. **Onboarding order: `phone` → `region` → `consent`.** `region` is asked only once a phone exists. **Every user now owes `consent`, existing ones included.**
5. **Region codes are validated against libphonenumber's `SUPPORTED_REGIONS`** (245 two-letter codes) rather than a second library; non-geographic `001` is excluded.
6. **`/capabilities` depends on `current_identity` instead of `current_household`** — the same resolution and commit (`current_household` is built on it), needed because the rule reads the user.
7. **The resolver no longer computes `onboarding_required`**; the route sets it from the shared rule.
8. **The `+` is optional.** Real Supabase tokens carry numbers without it; the fixtures use it. Both give the same answer, and tests cover both forms.
9. **"Terms in force"** = the newest global `account_terms` row whose `effective_from` has passed (NULL counts as effective). Per-market terms would add a country match in `current_terms`.

## Open questions / follow-ups

- **Release order — apply the migration to production *before* merging.** Render's deploys run no migrations (`render.yaml` has no pre-deploy step) and `main` deploys automatically. Merged first, the new code would query tables that do not exist yet, and `/me` would fail. The migration only adds (two tables, a column with a temporary default, seed rows), so it is safe to apply while the current code is still running — after approval, with the manager's go-ahead.
- **DRAFT legal copy** must be replaced before launch with counsel-approved text, **as new versions** — consent is logged against the rows seeded here (roadmap → Before launch).
- **The M1 demo app will show `consent` as an unknown onboarding step** until Humble-Coders/FinAI-Mobile-2026#16 ships the consent screen. Its `OnboardingStep` enum needs `REGION` and `CONSENT`.
- **For #16:** it consumes `terms`, `PUT /me/region`, `POST /me/consent` and `GET /legal/terms`. Open product question: whether the phone step's country dropdown should also send `PUT /me/region`, or only pick the dial code (the region then comes from the number, as here).
- **No dedicated concurrency test for derivation.** The conditional `UPDATE` handles the race by design, and the existing identity-concurrency tests still pass.
