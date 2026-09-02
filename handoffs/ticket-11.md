# Handoff — ticket #11

**Ticket:** [#11 — \[M1\] Wire authentication and bootstrap the household](https://github.com/Humble-Coders/Finance-backend/issues/11)
**Branch:** `ticket-11-auth-household-bootstrap` · **Base:** `main` · **11 files, +840 / −1** — of which 10 files / +754 are code and tests; the remainder is this report

## Summary

`GET /me` turns a verified Supabase JWT into exactly one user and one household, creating them on first call and never again. `app/auth.py` already verified tokens; nothing connected a verified token to our tables.

The hard part is **identity, not authentication**. One person may sign in three ways, and each produces a different Supabase `sub` — so treating each as a new person would split their financial history across households, which support cannot repair. Email cannot link them, because Apple's *Hide My Email* returns a relay address matching nothing else the person has used. The **verified phone can**, since every signup route ends with one, and that is the real reason the phone step is mandatory.

Also adds `current_household`, the dependency every later endpoint will use to scope data to the caller.

## Files changed

**New**
| File | Why |
|---|---|
| `app/services/identity.py` | Resolution as a service, not inline in the route — the logic is the ticket, and it needs testing without HTTP |
| `app/api/deps.py` | `current_identity` / `current_household` / `current_db_user`; maps the phone conflict to a coded 409 |
| `app/api/me.py` | `GET /me` |
| `app/schemas/identity.py` | Explicit response models, so the wire contract is a decision rather than whatever the ORM exposes |

**Modified**
| File | Why |
|---|---|
| `app/main.py` | Register the router |
| `tests/conftest.py` | `db_session` (rolled back, joined with `create_savepoint` so code under test may commit) and `api_client` (dependency overrides) |
| `tests/test_identity_resolution.py`, `tests/test_me_endpoint.py` | 24 tests across the four resolution branches, idempotency, Apple's once-only claims, and the conflict path |

**Not changed:** `app/auth.py` is untouched — verified against the diff. Token verification already worked; this ticket only consumes its output, which is why the 401 criterion is inherited rather than re-implemented.

## How to test

```bash
git checkout ticket-11-auth-household-bootstrap
source .venv/bin/activate && pytest -q
```

Expect **158 passed** with `.env` present; **121 passed, 37 skipped** without one.

The interesting behaviour, by hand — the Google-then-phone flow:

1. Sign in with Google (no phone) → `GET /me` returns 200 with `onboarding_required: ["phone"]` and `household.country_code: null`
2. Complete the phone step → `GET /me` returns `onboarding_required: []` and the same household id
3. A third account claiming that same phone → **409** with `code: "phone_already_linked"`, and the original owner is untouched

## Acceptance criteria

| Criterion | Status |
|---|---|
| `GET /me` returns the user and household | ✅ Met |
| Idempotent bootstrap — repeat calls never create a second household | ✅ Met |
| Persists email, phone, provider; tolerates a missing phone | ✅ Met |
| Apple's first-authorization email and name persisted, not overwritten later | ✅ Met — `TestAppleClaimsArriveOnlyOnce` |
| Identity resolution in the specified order | ✅ Met — all four branches covered |
| `onboarding_required: ["phone"]` without blocking | ✅ Met |
| Attaching an already-used phone fails cleanly and distinguishably | ✅ Met — 409, `phone_already_linked`, nothing merged |
| `country_code` left NULL, never guessed | ✅ Met |
| Reusable `current_household` dependency | ✅ Met |
| 401 for missing / malformed / expired tokens, never 500 | ✅ Met — unchanged behaviour in `app/auth.py`, which this ticket does not touch |
| Concurrent first requests produce one household | ⚠️ **Partial** — `ON CONFLICT DO NOTHING` on the provider identity is the mechanism, and it is used, but **no test exercises true concurrency**. See follow-ups |
| No endpoint reads another household's data | ✅ Met — `/me` resolves only through the token |
| `pytest` passes; CI green | ✅ Met — 158 pass locally; CI runs on the PR |

## Deviations / decisions

**1. `user.auth_user_id` is not a lookup key.** It records the *first* Supabase account seen for a person. When a second provider links by phone, that account's `sub` lives only in `user_identity` — so all resolution goes through `user_identity`. Looking up by `auth_user_id` would silently fail for anyone using more than one provider. Worth knowing before writing the next endpoint.

**2. Claims are absorbed, never erased.** Apple sends email and name on the first authorization only. A naive "update user from claims" would null them on the second sign-in and lose them permanently, so each field is only ever filled.

**3. The phone conflict check runs *before* absorbing claims.** Absorbing first leaves a pending UPDATE that autoflushes during the conflict lookup, so Postgres raises a raw `IntegrityError` before the friendly 409 can be produced. A test caught this; the fix is ordering, not error handling.

**4. `ON CONFLICT` infers its target from columns, not a constraint name.** The constraint is called `provider_identity`, not what the naming convention would generate — and a wrong name there fails only at runtime.

**5. Tests override `current_user` rather than minting real Supabase tokens.** Token verification is `app/auth.py`'s job and is covered separately; these tests are about what happens to an already-verified caller. This removed the ticket's "create a test user in Supabase" prerequisite.

**6. `GET /me` creates rows.** Flagged during planning: a GET with side effects is unconventional. It is a get-or-create, it is idempotent, and it saves every client a bootstrap round trip on the path users hit most.

**7. `app.main` is imported inside a function in `test_me_endpoint.py`, not at module level.** `app/main.py` configures FastAPI at import and so needs settings; a module-level import breaks *collection* on a runner with no configuration.

## Open questions / follow-ups

- **Concurrency is unproven.** `ON CONFLICT DO NOTHING` handles two simultaneous first requests, but no test fires them in parallel. The mechanism is right; the guarantee is untested. Worth a dedicated test before real signups.
- **One flaky failure was observed and not reproduced.** In one full-suite run, `test_the_conflict_does_not_merge_or_duplicate_anything` failed; it then passed in three subsequent runs, including two full-suite runs. Recorded rather than dismissed — if it recurs, suspect shared-connection contention against a cross-region database.
- **The database-backed suite takes about ten minutes**, against ~0.4s for the 121 that need no database. **Ticket #15** must keep them as separate CI jobs, or fast feedback disappears.
- **`app/auth.py` has no direct tests.** It is exercised indirectly and was not changed here, but the 401 behaviour is asserted nowhere. Worth a small ticket.
- **Region resolution is still ticket 2.1.** This ticket deliberately leaves `country_code` NULL even when a phone is present.
