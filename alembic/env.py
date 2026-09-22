"""Alembic environment. The database URL comes from settings, never alembic.ini."""

import asyncio
from logging.config import fileConfig
from uuid import uuid4

from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401 — autogenerate only sees imported models
from alembic import context
from app.config import get_settings
from app.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().migration_dsn,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # One transaction per revision, not one for the whole upgrade.
        #
        # Postgres refuses to *use* an enum value in the transaction that added
        # it — with one exception: it allows it when the enum type was created
        # in that same transaction. Applying from base, every migration shares
        # one transaction and the CREATE TYPE is in it, so the exception applies
        # and it works. Applying onto a database that already has the type, it
        # does not, and the upgrade fails with UnsafeNewEnumValueUsageError.
        #
        # That difference is invisible to CI, which only ever migrates from
        # base. It surfaced on production, where splitting the work across two
        # revisions was not enough because both revisions still ran inside one
        # transaction.
        #
        # The trade: a failure midway now leaves earlier revisions applied
        # rather than rolling the whole upgrade back. That is the standard
        # arrangement, and it is the honest one — each revision is a unit of
        # work, and a migration that cannot stand alone is a migration to split.
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(
        get_settings().migration_dsn,
        poolclass=NullPool,
        connect_args={
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
        },
    )
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
