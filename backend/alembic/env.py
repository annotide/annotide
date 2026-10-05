"""Alembic environment: async-capable, reads the DB URL from app settings."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import AsyncEngine, async_engine_from_config

from app.core.config import get_settings
from app.core.entra import aclose_token_sources
from app.db.base import Base
from app.db.session import engine_options

# Import every model module so Base.metadata is fully populated before
# Alembic compares it against the database (autogenerate) or emits DDL.
from app.models import *  # noqa: F403

# Alembic Config object, providing access to the values within alembic.ini.
config = context.config

# Interpret the config file for Python logging, unless run programmatically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The metadata object used for 'autogenerate' support.
target_metadata = Base.metadata

# Override the placeholder URL from alembic.ini with the real one from
# application settings (APP_DATABASE_URL), keeping secrets out of alembic.ini.
config.set_main_option("sqlalchemy.url", get_settings().database_url)


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode, emitting SQL without a live connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Configure the migration context against a live connection and run it."""
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations 'online' against it."""
    configuration: dict[str, Any] = config.get_section(config.config_ini_section, {})
    connectable: AsyncEngine = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # Entra sign-in (APP_DATABASE_AUTH=entra) supplies the password.
        connect_args=engine_options(get_settings()).get("connect_args", {}),
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()
    await aclose_token_sources()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode using the async engine."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
