"""Database engine and session management.

IMPORTANT — Supabase's transaction pooler (Supavisor) hands each statement to
whichever backend connection is free, so a prepared statement created by one
round trip may not exist on the next. All four settings below are required
together; three of them are not enough:

  * poolclass=NullPool                — let the pooler own pooling, not SQLAlchemy
  * statement_cache_size=0            — disable asyncpg's own cache
  * prepared_statement_cache_size=0   — disable SQLAlchemy's asyncpg-adapter cache
  * prepared_statement_name_func      — unique names per statement, so two
                                        connections can never collide

Without the name function you get, on the very first query:

    asyncpg.exceptions.InvalidSQLStatementNameError:
    prepared statement "__asyncpg_stmt_1__" does not exist

which SQLAlchemy surfaces as a bare ProgrammingError.

This is the configuration SQLAlchemy documents for pgbouncer-style poolers.
"""

from collections.abc import AsyncGenerator
from uuid import uuid4

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
    connect_args={
        "statement_cache_size": 0,
        "prepared_statement_cache_size": 0,
        "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
    },
    echo=False,
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a database session."""
    async with SessionLocal() as session:
        yield session
