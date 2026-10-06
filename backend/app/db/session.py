"""Async SQLAlchemy engine, sessionmaker and the ``get_session`` dependency."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from functools import lru_cache
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import BackingServiceAuth, Settings, get_settings
from app.core.entra import POSTGRES_SCOPE, token_source

#: With Entra sign-in, no pooled connection lives longer than this. Azure
#: checks the token only at sign-in, so an open connection would survive its
#: expiry; recycling well inside the shortest token lifetime (an hour) means
#: nothing depends on that.
ENTRA_POOL_RECYCLE_SECONDS = 45 * 60


def engine_options(settings: Settings) -> dict[str, Any]:
    """Engine keywords for `APP_DATABASE_AUTH` (shared with Alembic's env.py).

    With `entra`, asyncpg calls `password` for every new connection and gets
    the managed identity's current token; the URL names the Entra role.
    """
    if settings.database_auth is not BackingServiceAuth.ENTRA:
        return {}
    return {
        "connect_args": {"password": token_source(POSTGRES_SCOPE).password},
        "pool_recycle": ENTRA_POOL_RECYCLE_SECONDS,
    }


@lru_cache
def get_engine() -> AsyncEngine:
    """Lazily create the process-wide async engine from settings."""
    settings = get_settings()
    return create_async_engine(
        settings.database_url, pool_pre_ping=True, **engine_options(settings)
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Lazily create the process-wide async sessionmaker."""
    return async_sessionmaker(bind=get_engine(), expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI-style dependency yielding an ``AsyncSession`` per request."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        yield session
