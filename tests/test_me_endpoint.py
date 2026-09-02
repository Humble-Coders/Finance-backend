"""GET /me — the contract the mobile clients build against.

`current_user` is overridden rather than minting real Supabase tokens: token
verification is `app/auth.py`'s job and is covered separately. These tests are
about what the endpoint does with an already-verified caller.
"""

from __future__ import annotations

import uuid

import pytest

from app.auth import AuthenticatedUser, current_user
from tests.conftest import requires_db

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]


def authenticate_as(
    *, sub: str | None = None, provider: str = "phone", email=None, phone=None
) -> None:
    # Imported here, not at module level: `app.main` configures the FastAPI
    # instance at import and so needs settings. A module-level import would
    # break *collection* on a runner with no configuration — exactly the defect
    # tests/test_entrypoints.py exists to catch.
    from app.main import app

    caller = AuthenticatedUser(
        user_id=sub or str(uuid.uuid4()),
        email=email,
        phone=phone,
        claims={"app_metadata": {"provider": provider}},
    )
    app.dependency_overrides[current_user] = lambda: caller


class TestMe:
    async def test_returns_the_caller_and_their_household(self, api_client):
        authenticate_as(phone="+14165551001")
        response = await api_client.get("/me")

        assert response.status_code == 200
        body = response.json()
        assert body["user"]["phone"] == "+14165551001"
        assert body["household"]["id"]

    async def test_a_phone_user_owes_no_onboarding(self, api_client):
        authenticate_as(phone="+14165551002")
        body = (await api_client.get("/me")).json()
        assert body["onboarding_required"] == []

    async def test_a_google_user_is_told_to_collect_a_phone(self, api_client):
        """Not blocked — told. The client reads this and routes to the step."""
        authenticate_as(provider="google", email="g1@example.com")
        response = await api_client.get("/me")

        assert response.status_code == 200
        assert response.json()["onboarding_required"] == ["phone"]

    async def test_country_code_is_null_until_the_phone_arrives(self, api_client):
        authenticate_as(provider="google", email="g2@example.com")
        body = (await api_client.get("/me")).json()
        assert body["household"]["country_code"] is None

    async def test_repeat_calls_are_idempotent(self, api_client):
        sub = str(uuid.uuid4())
        authenticate_as(sub=sub, phone="+14165551003")

        first = (await api_client.get("/me")).json()
        second = (await api_client.get("/me")).json()

        assert first["household"]["id"] == second["household"]["id"]
        assert first["user"]["id"] == second["user"]["id"]

    async def test_onboarding_clears_once_the_phone_is_verified(self, api_client):
        """The Google-then-phone flow, end to end."""
        sub = str(uuid.uuid4())

        authenticate_as(sub=sub, provider="google", email="g3@example.com")
        assert (await api_client.get("/me")).json()["onboarding_required"] == ["phone"]

        authenticate_as(
            sub=sub, provider="google", email="g3@example.com", phone="+14165551004"
        )
        after = (await api_client.get("/me")).json()
        assert after["onboarding_required"] == []
        assert after["user"]["phone"] == "+14165551004"


class TestPhoneConflictResponse:
    async def test_returns_409_with_a_code_the_client_can_act_on(self, api_client):
        authenticate_as(phone="+14165551005")
        await api_client.get("/me")

        other = str(uuid.uuid4())
        authenticate_as(sub=other, provider="google", email="g4@example.com")
        await api_client.get("/me")

        authenticate_as(
            sub=other, provider="google", email="g4@example.com", phone="+14165551005"
        )
        response = await api_client.get("/me")

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "phone_already_linked"

    async def test_the_conflict_does_not_merge_or_duplicate_anything(self, api_client):
        authenticate_as(phone="+14165551006")
        original = (await api_client.get("/me")).json()

        other = str(uuid.uuid4())
        authenticate_as(sub=other, provider="google", email="g5@example.com")
        await api_client.get("/me")
        authenticate_as(
            sub=other, provider="google", email="g5@example.com", phone="+14165551006"
        )
        assert (await api_client.get("/me")).status_code == 409

        # The original owner is untouched.
        authenticate_as(phone="+14165551006")
        again = (await api_client.get("/me")).json()
        assert again["user"]["id"] == original["user"]["id"]
        assert again["household"]["id"] == original["household"]["id"]
