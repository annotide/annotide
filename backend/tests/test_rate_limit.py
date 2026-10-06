"""Tests for request rate limits (API-5): `services/rate_limit.py` and its enforcement.

The API tests run the real `get_current_user` and `/auth/login` against the
in-memory limiter `conftest.py` installs, with small limits injected
through the settings dependency.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.models import User
from app.services.rate_limit import (
    MemoryRateLimiter,
    RedisRateLimiter,
    login_identity,
    set_rate_limiter,
)
from tests.test_api_api_keys import (  # fixtures and helpers of the real-auth suite
    _add_user,
    _login,
    _mint,
    _seed_org,
    app,
    client,
    sessionmaker,
)

__all__ = ["app", "client", "sessionmaker"]  # re-exported pytest fixtures


def _limits(app: FastAPI, **values: Any) -> None:
    settings = get_settings().model_copy(update=values)
    app.dependency_overrides[get_settings] = lambda: settings


class TestLimiter:
    async def test_counts_per_window_and_reports_the_reset(self) -> None:
        limiter = MemoryRateLimiter(now=120.0 + 45)  # 45 s into a window
        first = await limiter.hit("user", "u1", 2)
        second = await limiter.hit("user", "u1", 2)
        third = await limiter.hit("user", "u1", 2)
        assert (first.allowed, first.remaining) == (True, 1)
        assert (second.allowed, second.remaining) == (True, 0)
        assert (third.allowed, third.reset_after) == (False, 15)
        assert (await limiter.hit("user", "u2", 2)).allowed  # another identity

        limiter.now = 180.0  # the next window starts over
        assert (await limiter.hit("user", "u1", 2)).remaining == 1

    async def test_redis_outage_fails_open(self) -> None:
        class _Broken:
            def pipeline(self, transaction: bool = True) -> Any:
                raise RedisConnectionError("down")

            async def aclose(self) -> None: ...

        decision = await RedisRateLimiter(_Broken()).hit("user", "u1", 5)  # type: ignore[arg-type]  # a stand-in with only what hit() touches
        assert decision.allowed and decision.remaining == 5

    def test_login_identity_never_holds_the_email(self) -> None:
        identity = login_identity("10.0.0.1", " Person@Example.com ")
        assert identity.startswith("10.0.0.1:")
        assert "example" not in identity.lower()
        assert identity == login_identity("10.0.0.1", "person@example.com")


class TestEnforcement:
    async def test_a_login_token_is_limited_per_user_with_retry_after(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _limits(app, rate_limit_per_minute=2)
        org_id = await _seed_org(sessionmaker)
        user = await _add_user(sessionmaker, org_id)
        other = await _add_user(sessionmaker, org_id)

        assert client.get("/api/v1/auth/me", headers=_login(user)).status_code == 200
        assert client.get("/api/v1/auth/me", headers=_login(user)).status_code == 200
        limited = client.get("/api/v1/auth/me", headers=_login(user))
        assert limited.status_code == 429
        assert limited.json()["type"].endswith("rate-limited")
        assert 1 <= int(limited.headers["Retry-After"]) <= 60
        assert limited.json()["retry_after"] == int(limited.headers["Retry-After"])
        # Someone else's budget is their own.
        assert client.get("/api/v1/auth/me", headers=_login(other)).status_code == 200

    async def test_every_counted_response_carries_ratelimit_headers(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _limits(app, rate_limit_per_minute=2)
        user = await _add_user(sessionmaker, await _seed_org(sessionmaker))

        first = client.get("/api/v1/auth/me", headers=_login(user))
        second = client.get("/api/v1/auth/me", headers=_login(user))
        limited = client.get("/api/v1/auth/me", headers=_login(user))

        assert first.headers["RateLimit-Limit"] == "2"
        assert first.headers["RateLimit-Remaining"] == "1"
        assert second.headers["RateLimit-Remaining"] == "0"
        assert 1 <= int(first.headers["RateLimit-Reset"]) <= 60
        assert limited.status_code == 429
        assert limited.headers["RateLimit-Remaining"] == "0"
        assert limited.headers["RateLimit-Reset"] == limited.headers["Retry-After"]
        # Nothing counted, nothing reported.
        assert "RateLimit-Limit" not in client.get("/api/v1/health").headers

    async def test_an_api_key_is_limited_per_key_not_per_user(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        user = await _add_user(sessionmaker, org_id)
        _, first = _mint(client, user, name="one")
        _, second = _mint(client, user, name="two")
        _limits(app, rate_limit_api_key_per_minute=1, rate_limit_per_minute=100)

        assert client.get("/api/v1/auth/me", headers=first).status_code == 200
        assert client.get("/api/v1/auth/me", headers=first).status_code == 429
        assert client.get("/api/v1/auth/me", headers=second).status_code == 200
        assert client.get("/api/v1/auth/me", headers=_login(user)).status_code == 200

    async def test_login_is_limited_before_the_password_is_checked(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _limits(app, rate_limit_login_per_minute=2)
        org_id = await _seed_org(sessionmaker)
        user = await _add_user(sessionmaker, org_id)
        async with sessionmaker() as session:
            row = await session.get(User, user.id)
            assert row is not None
            row.password_hash = hash_password("right-password")
            await session.commit()

        wrong = {"email": user.email, "password": "wrong"}
        assert client.post("/api/v1/auth/login", json=wrong).status_code == 401
        assert client.post("/api/v1/auth/login", json=wrong).status_code == 401
        # Even the right password waits for the next window.
        right = {"email": user.email, "password": "right-password"}
        assert client.post("/api/v1/auth/login", json=right).status_code == 429
        # A different e-mail from the same address has its own counter.
        stranger = {"email": "nobody@example.com", "password": "x"}
        assert client.post("/api/v1/auth/login", json=stranger).status_code == 401

    async def test_disabled_means_unlimited(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _limits(app, rate_limit_enabled=False, rate_limit_per_minute=1)
        org_id = await _seed_org(sessionmaker)
        user = await _add_user(sessionmaker, org_id)
        for _ in range(3):
            assert client.get("/api/v1/auth/me", headers=_login(user)).status_code == 200


@pytest.fixture(autouse=True)
def _fresh_limiter() -> None:
    set_rate_limiter(MemoryRateLimiter())


def test_settings_defaults() -> None:
    settings = Settings()
    assert settings.rate_limit_enabled is True
    assert settings.rate_limit_per_minute == 1200
    assert settings.rate_limit_api_key_per_minute == 1200
    assert settings.rate_limit_login_per_minute == 10
