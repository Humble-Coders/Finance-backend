"""GET /capabilities and require_feature — the shown/allowed pair.

The payload hides a feature; `require_feature` refuses it. Both are needed: a
client is not a security boundary, and anyone holding a valid token can call an
endpoint directly.
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


def authenticate_as(*, sub=None, provider="phone", email=None, phone=None) -> None:
    from app.main import app

    caller = AuthenticatedUser(
        user_id=sub or str(uuid.uuid4()),
        email=email,
        phone=phone,
        claims={"app_metadata": {"provider": provider}},
    )
    app.dependency_overrides[current_user] = lambda: caller


class TestEndpoint:
    async def test_a_phone_user_gets_their_region(self, api_client):
        authenticate_as(phone="+14165570001")
        # /me establishes the household; region stays NULL until 2.1 derives it.
        await api_client.get("/me")

        response = await api_client.get("/capabilities")
        assert response.status_code == 200
        body = response.json()
        assert "features" in body and body["features"]

    async def test_an_unknown_region_still_returns_200(self, api_client):
        """The window before the phone step is normal, not an error."""
        authenticate_as(provider="google", email="cap1@example.com")
        await api_client.get("/me")

        response = await api_client.get("/capabilities")
        assert response.status_code == 200
        body = response.json()
        assert body["region"] is None
        assert body["onboarding_required"] == ["phone"]

    async def test_features_are_never_absent(self, api_client):
        """A client cannot tell "feature missing" from "feature off"."""
        authenticate_as(phone="+14165570002")
        await api_client.get("/me")

        features = (await api_client.get("/capabilities")).json()["features"]
        for key in ("bank_linking", "document_upload", "ai_chat"):
            assert key in features, f"{key} absent from the payload"

    async def test_a_disabled_feature_explains_itself(self, api_client):
        authenticate_as(phone="+14165570003")
        await api_client.get("/me")

        features = (await api_client.get("/capabilities")).json()["features"]
        assert features["bank_linking"]["enabled"] is False
        assert features["bank_linking"]["reason"] is not None


class TestRequireFeature:
    """Enforcement, not decoration.

    Mounted on a throwaway route so the pattern is proven before there is a real
    feature to gate — retrofitting enforcement after endpoints exist is how a
    hidden button becomes an open endpoint.
    """

    def _mount(self, feature_key: str) -> str:
        from app.main import app
        from app.services.capabilities import require_feature

        path = f"/__test__/{feature_key}-{uuid.uuid4().hex[:6]}"

        @app.get(
            path,
            dependencies=[__import__("fastapi").Depends(require_feature(feature_key))],
        )
        async def _guarded() -> dict[str, bool]:
            return {"reached": True}

        return path

    async def test_passes_through_when_enabled(self, api_client):
        authenticate_as(phone="+14165570004")
        await api_client.get("/me")

        path = self._mount("document_upload")  # seeded enabled
        response = await api_client.get(path)
        assert response.status_code == 200
        assert response.json() == {"reached": True}

    async def test_returns_403_when_disabled(self, api_client):
        authenticate_as(phone="+14165570005")
        await api_client.get("/me")

        path = self._mount("bank_linking")  # seeded disabled
        response = await api_client.get(path)
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "feature_unavailable"
        assert response.json()["detail"]["feature"] == "bank_linking"

    async def test_an_unknown_feature_is_refused_not_allowed(self, api_client):
        """Fail closed: a typo in a feature key must not open an endpoint."""
        authenticate_as(phone="+14165570006")
        await api_client.get("/me")

        path = self._mount(f"never_defined_{uuid.uuid4().hex[:6]}")
        response = await api_client.get(path)
        assert response.status_code == 403
        # Names our missing row rather than the caller's region — the reason is
        # the first thing whoever debugs this reads.
        assert response.json()["detail"]["reason"] == "unknown_feature"
