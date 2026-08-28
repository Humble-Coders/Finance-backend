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

from app.config import get_settings


class Base(DeclarativeBase):
    """Base class for all ORM models."""


_settings = get_settings()

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
