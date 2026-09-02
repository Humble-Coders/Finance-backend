"""Two first requests arriving at once must produce one household.

This needs its own module because it cannot use the shared rolled-back session:
the two requests have to be genuinely concurrent on separate connections, and
they have to commit for the race to exist at all. Rows are therefore real, and
each test cleans up after itself.

Two devices, or a client retrying a slow request, is enough to trigger this — on
the signup path, where a 500 is least forgivable.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import AuthenticatedUser
from app.services.identity import resolve_user
from tests.conftest import requires_db

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="session"),
    requires_db,
]


def _caller(sub: str) -> AuthenticatedUser:
    return AuthenticatedUser(
        user_id=sub,
        email=None,
        phone=None,
        claims={"app_metadata": {"provider": "google"}},
    )


async def _resolve_and_commit(engine, sub: str):
    async with AsyncSession(engine, expire_on_commit=False) as session:
        resolved = await resolve_user(session, _caller(sub))
        await session.commit()
        return resolved


async def _cleanup(engine, sub: str) -> None:
    async with AsyncSession(engine) as session:
        await session.execute(
            text("DELETE FROM user_identity WHERE provider_user_id = :sub"),
            {"sub": sub},
        )
        await session.execute(
            text(
                "DELETE FROM household WHERE id IN "
                '(SELECT household_id FROM "user" WHERE auth_user_id = :sub)'
            ),
            {"sub": sub},
        )
        await session.commit()


class TestConcurrentFirstRequest:
    async def test_neither_request_errors(self):
        """Before the retry was added, the loser raised IntegrityError.

        `user.auth_user_id` is UNIQUE, so the database always protected the
        data — but the losing request surfaced a 500 to a real person on their
        first ever call.
        """
        from app.db import get_engine

        engine = get_engine()
        sub = str(uuid.uuid4())
        try:
            results = await asyncio.gather(
                _resolve_and_commit(engine, sub),
                _resolve_and_commit(engine, sub),
                return_exceptions=True,
            )
            failures = [r for r in results if isinstance(r, BaseException)]
            assert failures == [], f"a concurrent first request failed: {failures}"
        finally:
            await _cleanup(engine, sub)

    async def test_both_requests_see_the_same_household(self):
        from app.db import get_engine

        engine = get_engine()
        sub = str(uuid.uuid4())
        try:
            first, second = await asyncio.gather(
                _resolve_and_commit(engine, sub),
                _resolve_and_commit(engine, sub),
            )
            assert first.household.id == second.household.id
            assert first.user.id == second.user.id
        finally:
            await _cleanup(engine, sub)

    async def test_exactly_one_household_and_one_user_exist(self):
        from app.db import get_engine

        engine = get_engine()
        sub = str(uuid.uuid4())
        try:
            await asyncio.gather(
                _resolve_and_commit(engine, sub),
                _resolve_and_commit(engine, sub),
            )
            async with AsyncSession(engine) as session:
                users = await session.scalar(
                    text('SELECT count(*) FROM "user" WHERE auth_user_id = :sub'),
                    {"sub": sub},
                )
                identities = await session.scalar(
                    text(
                        "SELECT count(*) FROM user_identity "
                        "WHERE provider_user_id = :sub"
                    ),
                    {"sub": sub},
                )
            assert users == 1
            assert identities == 1, "the losing request must not orphan an identity row"
        finally:
            await _cleanup(engine, sub)
