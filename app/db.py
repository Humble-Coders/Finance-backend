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

The engine is built **lazily**, on first use. Building it at import time meant
importing anything that touches this module — including the ORM models — failed
outright when no configuration was present, because Settings requires
DATABASE_URL. That made schema tests, which need no database at all,
unrunnable on a machine or CI runner without a .env.
"""

from collections.abc import AsyncGenerator
from functools import lru_cache
from uuid import uuid4

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

import structlog

from app.config import get_settings
from app.core.dsn import describe_dsn

log = structlog.get_logger()


# Postgres names constraints for us when we don't, and those generated names are
# not stable — which means Alembic cannot reliably drop them, and downgrade()
# breaks at exactly the moment it is needed. Fixing the convention now, before
# the first migration, is far cheaper than renaming constraints later.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


@lru_cache
def get_engine() -> AsyncEngine:
    """The application engine, created once on first use.

    Deliberately not created at import: `Settings` requires DATABASE_URL, so an
    import-time engine makes `import app.models` fail wherever no configuration
    exists — which is every CI runner, and any developer without a .env.
    Nothing here opens a connection; that happens on first query.
    """
    settings = get_settings()

    # Logged once, when the engine is actually configured, so a deployed
    # instance can be checked against its config without guessing.
    # Credentials are stripped by describe_dsn.
    log.info("database_configured", dsn=describe_dsn(settings.database_dsn))

    return create_async_engine(
        settings.database_dsn,
        poolclass=NullPool,
        connect_args={
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
        },
        echo=False,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """The session factory, bound to the lazily-created engine."""
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a database session."""
    async with get_sessionmaker()() as session:
        yield session
