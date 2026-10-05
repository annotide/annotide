"""Shared test fixtures.

The suite runs without a live PostgreSQL or Redis: anything that would touch a
real dependency is overridden per test. Tests that genuinely need a database are
marked ``@pytest.mark.integration`` and skipped unless ``APP_DATABASE_URL``
points at a reachable server (CI provides one via service containers).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

# Settings are read at import time by several modules, so seed the environment
# before anything under `app.` is imported.
os.environ.setdefault("APP_DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("APP_SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("APP_LOG_FORMAT", "console")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: needs a live PostgreSQL and Redis; skipped by default"
    )


@pytest.fixture
def settings() -> Iterator[object]:
    """A fresh Settings instance with the cache cleared around the test."""
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _memory_rate_limiter() -> Iterator[object]:
    """Every test gets fresh in-process rate-limit counters instead of Redis (API-5)."""
    from app.services.rate_limit import MemoryRateLimiter, set_rate_limiter

    limiter = MemoryRateLimiter()
    set_rate_limiter(limiter)
    yield limiter
    set_rate_limiter(None)


@pytest.fixture(autouse=True)
def _no_shared_job_queue() -> Iterator[None]:
    """No test inherits the process-wide job queue another test opened.

    A queue connected in one test's event loop cannot be closed from another's:
    the next app shutdown (`aclose_job_queue`) would fail with "Event loop is
    closed" wherever Redis is reachable, as in CI. The stale pool is dropped,
    not closed, for the same reason.
    """
    from app.services.queue import set_job_queue

    set_job_queue(None)
    yield
    set_job_queue(None)


@pytest.fixture(autouse=True)
def _fresh_connector_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test leases a connector instance another test built (or patched)."""
    from app.services import storage

    monkeypatch.setattr(storage, "_pool", storage.ConnectorPool())


@pytest.fixture(autouse=True)
def _unkeyed_build() -> Iterator[None]:
    """Every test runs as a build with no vendor key, unless it adds its own.

    `keys.py` carries the production public key; with it, a test without a
    licence would run as Community (three users, no Business features). Tests
    that exercise licensing add their own key with `monkeypatch.setitem`, which
    is undone before this restores the production mapping.
    """
    from app.services.licensing import keys

    production = dict(keys.VENDOR_PUBLIC_KEYS)
    keys.VENDOR_PUBLIC_KEYS.clear()
    yield
    keys.VENDOR_PUBLIC_KEYS.clear()
    keys.VENDOR_PUBLIC_KEYS.update(production)
