"""Fixtures for database-backed tests.

Every test runs inside a transaction that is **always rolled back**, so the
suite can be pointed at a real database — including production, which is where
it has to run until a staging environment exists — without leaving anything
behind.
"""

from __future__ import annotations

import pytest
import pytest_asyncio


def _database_is_configured() -> bool:
    """Whether a usable DSN exists.

    Reads through Settings rather than os.getenv: the value normally comes from
    a .env file, so checking the OS environment alone reports "not configured"
    on a machine that is perfectly well configured — and these tests would then
    skip silently, which is worse than failing.
    """
    try:
        from app.config import get_settings

        return bool(get_settings().database_dsn)
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not _database_is_configured(),
    reason="no database configured (set DATABASE_URL in .env or the environment)",
)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def _connection():
    """One connection for the whole suite.

    Opening one per test exhausted the pooler — ~30 tests ran it dry with
    `ECHECKOUTTIMEOUT`. Sharing one connection also cuts runtime substantially,
    since each connect was a round trip to another region.

    Uses the **runtime** DSN (transaction pooler), not the migration one. These
    tests only do DML, so session mode buys nothing — and the session pool is
    small and needed for migrations. This also means the tests exercise the same
    connection path the application uses.
    """
    import asyncpg
    from sqlalchemy.engine import make_url

    from app.config import get_settings

    url = make_url(get_settings().database_dsn)
    connection = await asyncpg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        database=url.database,
        # The pooler does not support prepared statements; asyncpg caches them
        # by default, which fails intermittently under a shared connection.
        statement_cache_size=0,
    )
    try:
        yield connection
    finally:
        await connection.close()


@pytest_asyncio.fixture(loop_scope="session")
async def db(_connection):
    """The shared connection, wrapped in a transaction that always rolls back.

    Isolation still holds: each test sees only its own uncommitted work, and
    nothing survives the test.
    """
    transaction = _connection.transaction()
    await transaction.start()
    try:
        yield _connection
    finally:
        await transaction.rollback()


@pytest_asyncio.fixture(loop_scope="session")
async def db_session():
    """An AsyncSession whose work is rolled back when the test ends.

    Bound to an explicit connection with an outer transaction, and joined with
    `create_savepoint`, so code under test may call `session.commit()` — which
    `current_identity` does — without escaping the rollback. Nothing reaches the
    real database permanently.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.db import get_engine

    engine = get_engine()
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


@pytest_asyncio.fixture(loop_scope="session")
async def api_client(db_session):
    """An HTTP client against the real app, with two dependencies overridden.

    `get_session` points at the rolled-back test session. `current_user` is
    overridden per-test via `authenticate_as`, so these tests exercise identity
    resolution rather than re-testing signature verification — `app/auth.py`
    is covered separately and needs no live Supabase token here.
    """
    from httpx import ASGITransport, AsyncClient

    from app.auth import current_user
    from app.db import get_session
    from app.main import app

    async def _session_override():
        yield db_session

    app.dependency_overrides[get_session] = _session_override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        client.app = app  # so tests can install their own current_user override
        yield client
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(current_user, None)
