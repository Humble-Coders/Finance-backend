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

        return bool(get_settings().migration_dsn)
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not _database_is_configured(),
    reason="no database configured (set DATABASE_URL in .env or the environment)",
)


@pytest_asyncio.fixture
async def db():
    """An asyncpg connection whose work is rolled back when the test ends."""
    import asyncpg
    from sqlalchemy.engine import make_url

    from app.config import get_settings

    url = make_url(get_settings().migration_dsn)
    connection = await asyncpg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        database=url.database,
    )
    transaction = connection.transaction()
    await transaction.start()
    try:
        yield connection
    finally:
        await transaction.rollback()
        await connection.close()
