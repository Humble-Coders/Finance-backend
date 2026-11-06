"""Profile's account writes: the name, and deleting the account (2026-11-06).

Deleting is a hard delete (PRD Appendix A.5): the household's rows go through
the database's cascades, the sign-in through Supabase, and nothing goes
unless both do.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.models.identity import ConsentEvent, Household, User
from app.models.money import Transaction
from app.services import account_deletion
from app.services.account_deletion import SignInDeletionError, delete_sign_in
from tests.conftest import requires_db
from tests.test_dashboard import a_household, tx

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]


@pytest.fixture
def removed(monkeypatch) -> list[str]:
    """Supabase, faked: the sign-ins it was asked to remove."""
    asked: list[str] = []

    async def fake(_settings, auth_user_id: str) -> None:
        asked.append(auth_user_id)

    monkeypatch.setattr(account_deletion, "delete_sign_in", fake)
    return asked


async def count(db_session, model, **where) -> int:
    query = select(func.count()).select_from(model)
    for column, value in where.items():
        query = query.where(getattr(model, column) == value)
    return await db_session.scalar(query)


class TestTheName:
    async def test_is_saved_trimmed(self, api_client):
        await a_household(api_client, "+14165590101")

        response = await api_client.patch("/me", json={"display_name": "  Sam Rivera "})

        assert response.status_code == 200
        assert response.json()["user"]["display_name"] == "Sam Rivera"

    async def test_blank_is_refused(self, api_client):
        await a_household(api_client, "+14165590102")

        assert (
            await api_client.patch("/me", json={"display_name": ""})
        ).status_code == 422
        assert (
            await api_client.patch("/me", json={"display_name": "   "})
        ).status_code == 422


class TestDeletingTheAccount:
    async def test_everything_goes_and_so_does_the_sign_in(
        self, api_client, db_session, removed
    ):
        household, account = await a_household(api_client, "+14165590103")
        db_session.add(tx(household, account, minor=1_000))
        await db_session.commit()
        me = (await api_client.get("/me")).json()
        user = await db_session.get(User, uuid.UUID(me["user"]["id"]))
        auth_user_id, user_id = user.auth_user_id, user.id

        response = await api_client.delete("/me")

        assert response.status_code == 204
        assert removed == [auth_user_id]
        db_session.expire_all()
        assert await count(db_session, Household, id=household) == 0
        assert await count(db_session, Transaction, household_id=household) == 0
        assert await count(db_session, User, id=user_id) == 0
        assert await count(db_session, ConsentEvent, user_id=user_id) == 0

    async def test_nothing_goes_when_the_sign_in_cannot_be_removed(
        self, api_client, db_session, monkeypatch
    ):
        household, account = await a_household(api_client, "+14165590104")
        db_session.add(tx(household, account, minor=1_000))
        await db_session.commit()

        async def refuse(_settings, _auth_user_id: str) -> None:
            raise SignInDeletionError("supabase answered 500")

        monkeypatch.setattr(account_deletion, "delete_sign_in", refuse)

        response = await api_client.delete("/me")

        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "deletion_unavailable"
        db_session.expire_all()
        assert await count(db_session, Household, id=household) == 1
        assert await count(db_session, Transaction, household_id=household) == 1

    async def test_a_shared_household_keeps_its_rows(
        self, api_client, db_session, removed
    ):
        household, account = await a_household(api_client, "+14165590105")
        db_session.add(tx(household, account, minor=1_000))
        partner = User(
            household_id=household,
            auth_user_id=f"auth-{uuid.uuid4()}",
            phone="+14165590106",
        )
        db_session.add(partner)
        await db_session.commit()

        assert (await api_client.delete("/me")).status_code == 204

        db_session.expire_all()
        assert await count(db_session, Household, id=household) == 1
        assert await count(db_session, Transaction, household_id=household) == 1
        assert await count(db_session, User, household_id=household) == 1


def settings(key: str = "service-key") -> Settings:
    return Settings(
        database_url="postgresql://localhost/x",
        supabase_url="https://example.supabase.co",
        supabase_service_role_key=key,
    )


def answering(monkeypatch, status: int, seen: list[httpx.Request]) -> None:
    real = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)

    monkeypatch.setattr(
        account_deletion.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )


class TestTheSupabaseCall:
    async def test_uses_the_admin_endpoint_with_the_service_key(self, monkeypatch):
        seen: list[httpx.Request] = []
        answering(monkeypatch, 200, seen)

        await delete_sign_in(settings(), "auth-123")

        assert seen[0].method == "DELETE"
        assert (
            str(seen[0].url)
            == "https://example.supabase.co/auth/v1/admin/users/auth-123"
        )
        assert seen[0].headers["Authorization"] == "Bearer service-key"

    async def test_already_gone_counts_as_done(self, monkeypatch):
        answering(monkeypatch, 404, [])
        await delete_sign_in(settings(), "auth-123")

    async def test_a_refusal_or_no_key_raises(self, monkeypatch):
        answering(monkeypatch, 500, [])
        with pytest.raises(SignInDeletionError):
            await delete_sign_in(settings(), "auth-123")
        with pytest.raises(SignInDeletionError):
            await delete_sign_in(settings(key=""), "auth-123")
