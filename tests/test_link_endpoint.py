"""POST /me/link — removing the empty account a new sign-in method created.

The caller, the second-token verifier and the Supabase admin client are all
overridden. These tests are about what the endpoint decides, and must never
reach Supabase. Every test rolls back.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.auth import AuthenticatedUser, current_user, get_token_verifier
from app.models.identity import User
from app.models.setup import FinancialProfile
from app.services.supabase_admin import SupabaseAdminError, get_supabase_admin
from tests.conftest import requires_db

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]

LINK = "/me/link"


def identity(
    *, sub=None, provider="phone", email=None, phone=None
) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=sub or str(uuid.uuid4()),
        email=email,
        phone=phone,
        claims={"app_metadata": {"provider": provider}},
    )


class FakeAdmin:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.fail = False

    async def delete_auth_user(self, auth_user_id: str) -> None:
        if self.fail:
            raise SupabaseAdminError("simulated outage")
        self.deleted.append(auth_user_id)


class World:
    def __init__(self, app) -> None:
        self.app = app
        self.tokens: dict[str, AuthenticatedUser] = {}
        self.admin = FakeAdmin()

    def act_as(self, who: AuthenticatedUser) -> None:
        self.app.dependency_overrides[current_user] = lambda: who


@pytest_asyncio.fixture(loop_scope="session")
async def world(api_client):
    from fastapi import HTTPException

    from app.main import app

    w = World(app)

    def verify(token: str) -> AuthenticatedUser:
        if token not in w.tokens:
            raise HTTPException(status_code=401, detail="token verification failed")
        return w.tokens[token]

    app.dependency_overrides[get_token_verifier] = lambda: verify
    app.dependency_overrides[get_supabase_admin] = lambda: w.admin
    yield w
    app.dependency_overrides.pop(get_token_verifier, None)
    app.dependency_overrides.pop(get_supabase_admin, None)


async def sign_in(client, world: World, who: AuthenticatedUser) -> dict:
    world.act_as(who)
    response = await client.get("/me")
    assert response.status_code == 200, response.text
    return response.json()


async def user_for(db_session, auth_user_id: str) -> User | None:
    result = await db_session.execute(
        select(User).where(User.auth_user_id == auth_user_id)
    )
    return result.scalar_one_or_none()


class TestRemovingTheOrphan:
    async def test_removes_its_rows_and_its_supabase_account(
        self, api_client, world, db_session
    ):
        owner = identity(phone="+14165580201")
        await sign_in(api_client, world, owner)
        orphan = identity(provider="google", email="orphan1@example.com")
        await sign_in(api_client, world, orphan)
        world.tokens["orphan-token"] = orphan

        world.act_as(owner)
        response = await api_client.post(LINK, json={"orphan_token": "orphan-token"})

        assert response.status_code == 200, response.text
        assert response.json()["user"]["phone"] == "+14165580201"
        assert world.admin.deleted == [orphan.user_id]
        assert await user_for(db_session, orphan.user_id) is None

    async def test_an_orphan_that_never_reached_the_api_is_still_deleted(
        self, api_client, world
    ):
        owner = identity(phone="+14165580202")
        await sign_in(api_client, world, owner)
        orphan = identity(provider="apple", email="relay@privaterelay.appleid.com")
        world.tokens["t"] = orphan  # signed in to Supabase, never called /me

        world.act_as(owner)
        response = await api_client.post(LINK, json={"orphan_token": "t"})

        assert response.status_code == 200, response.text
        assert world.admin.deleted == [orphan.user_id]


class TestRefusals:
    async def test_a_token_that_does_not_verify_is_422_and_touches_nothing(
        self, api_client, world
    ):
        owner = identity(phone="+14165580203")
        await sign_in(api_client, world, owner)

        world.act_as(owner)
        response = await api_client.post(LINK, json={"orphan_token": "forged"})

        # Not 401: the caller's own session is valid, and clients treat a 401
        # as their session ending.
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "invalid_orphan_token"
        assert world.admin.deleted == []

    async def test_an_account_with_a_verified_phone_is_not_an_orphan(
        self, api_client, world, db_session
    ):
        owner = identity(phone="+14165580204")
        await sign_in(api_client, world, owner)
        other = identity(
            provider="google", email="real2@example.com", phone="+14165580205"
        )
        await sign_in(api_client, world, other)
        world.tokens["t"] = other

        world.act_as(owner)
        response = await api_client.post(LINK, json={"orphan_token": "t"})

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "orphan_not_empty"
        assert world.admin.deleted == []
        assert await user_for(db_session, other.user_id) is not None

    async def test_an_account_holding_financial_data_is_not_an_orphan(
        self, api_client, world, db_session
    ):
        """Checked, not assumed — even though onboarding should make it impossible."""
        owner = identity(phone="+14165580206")
        await sign_in(api_client, world, owner)
        orphan = identity(provider="google", email="data@example.com")
        me = await sign_in(api_client, world, orphan)
        db_session.add(
            FinancialProfile(
                household_id=uuid.UUID(me["household"]["id"]), currency="CAD"
            )
        )
        await db_session.flush()
        world.tokens["t"] = orphan

        world.act_as(owner)
        response = await api_client.post(LINK, json={"orphan_token": "t"})

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "orphan_not_empty"
        assert world.admin.deleted == []

    async def test_the_account_linked_into_must_have_verified_its_phone(
        self, api_client, world
    ):
        owner = identity(provider="google", email="nophone@example.com")
        await sign_in(api_client, world, owner)
        orphan = identity(provider="apple", email="o3@example.com")
        await sign_in(api_client, world, orphan)
        world.tokens["t"] = orphan

        world.act_as(owner)
        response = await api_client.post(LINK, json={"orphan_token": "t"})

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "link_target_incomplete"

    async def test_linking_an_account_into_itself_is_refused(self, api_client, world):
        owner = identity(phone="+14165580207")
        await sign_in(api_client, world, owner)
        world.tokens["self"] = owner

        world.act_as(owner)
        response = await api_client.post(LINK, json={"orphan_token": "self"})

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "nothing_to_link"
        assert world.admin.deleted == []


class TestCleanupFailure:
    async def test_a_failed_supabase_delete_can_be_retried(
        self, api_client, world, db_session
    ):
        owner = identity(phone="+14165580208")
        await sign_in(api_client, world, owner)
        orphan = identity(provider="google", email="o4@example.com")
        await sign_in(api_client, world, orphan)
        world.tokens["t"] = orphan

        world.admin.fail = True
        world.act_as(owner)
        first = await api_client.post(LINK, json={"orphan_token": "t"})
        assert first.status_code == 502
        assert first.json()["detail"]["code"] == "orphan_auth_cleanup_failed"
        # The database side went first, so a retry has only the delete left.
        assert await user_for(db_session, orphan.user_id) is None

        world.admin.fail = False
        second = await api_client.post(LINK, json={"orphan_token": "t"})
        assert second.status_code == 200, second.text
        assert world.admin.deleted == [orphan.user_id]
