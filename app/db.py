"""Database engine and session management.

IMPORTANT — Supabase transaction pooler (Supavisor) does not support prepared
statements. asyncpg caches them by default, which produces confusing
`DuplicatePreparedStatementError` failures under load. Both settings below are
required, not optional:

  * statement_cache_size=0   — disable asyncpg's prepared-statement cache
  * poolclass=NullPool       — let the pooler own pooling, not SQLAlchemy
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

import structlog

from app.config import get_settings
from app.core.dsn import describe_dsn

log = structlog.get_logger()


class Base(DeclarativeBase):
    """Base class for all ORM models."""


_settings = get_settings()

# Logged at import so a deployed instance can be checked against its config
# without guessing. Credentials are stripped by describe_dsn.
log.info("database_configured", dsn=describe_dsn(_settings.database_dsn))

engine = create_async_engine(
    _settings.database_dsn,
    poolclass=NullPool,
    connect_args={"statement_cache_size": 0, "prepared_statement_cache_size": 0},
    echo=False,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a database session."""
    async with SessionLocal() as session:
        yield session
