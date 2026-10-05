"""Tests for `GET /license` (LIC-1).

Builds a standalone app around just this router and overrides auth/settings
the same way `test_api_licensing.py` does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import date
from typing import Any, cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser, SettingsDep, get_current_user, get_effective_license
from app.api.errors import register_exception_handlers
from app.api.v1.license import _active_users
from app.api.v1.license import router as license_router
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.session import get_session
from app.models import LicenseState
from app.services.licensing import license as license_mod
from app.services.licensing.features import BUSINESS_FEATURES
from app.services.licensing.issue import generate_keypair, issue
from app.services.licensing.state import EffectiveLicense, KeySource, resolve

_TABLES: list[Table] = [cast(Table, LicenseState.__table__)]


def admin_user() -> CurrentUser:
    return CurrentUser(
        id=uuid4(),
        organization_id=uuid4(),
        email="admin@acme.com",
        is_superuser=True,
        is_service=False,
        scopes=frozenset(),
    )


def plain_user() -> CurrentUser:
    return CurrentUser(
        id=uuid4(),
        organization_id=uuid4(),
        email="anna@acme.com",
        is_superuser=False,
        is_service=False,
        scopes=frozenset(),
    )


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://t:t@localhost/t",
        "secret_key": "test-key",
    }
    base.update(overrides)
    return Settings(**base)


async def _licence_from_settings(settings: SettingsDep) -> EffectiveLicense:
    """`get_effective_license` without a database: the env key is the only source."""
    return resolve([(KeySource.ENV, settings.license_key)], today=date.today())


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def app(sessionmaker: async_sessionmaker[AsyncSession]) -> Iterator[FastAPI]:
    application = FastAPI()
    register_exception_handlers(application)
    application.include_router(license_router, prefix="/api/v1")

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_session] = _get_session
    application.dependency_overrides[get_current_user] = admin_user
    application.dependency_overrides[get_effective_license] = _licence_from_settings
    application.dependency_overrides[get_settings] = lambda: make_settings()
    application.dependency_overrides[_active_users] = lambda: 3
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


class TestGetLicense:
    def test_requires_a_superuser(self, app: FastAPI, client: TestClient) -> None:
        app.dependency_overrides[get_current_user] = plain_user
        assert client.get("/api/v1/license").status_code == 403

    def test_no_key_is_community(self, client: TestClient) -> None:
        body = client.get("/api/v1/license").json()
        assert body == {
            "status": "community",
            "tier": "community",
            "licensee": None,
            "seats": None,
            "expires_at": None,
            "features": list(BUSINESS_FEATURES),
            "business_features": list(BUSINESS_FEATURES),
            "owner_only": False,
            "trial_used": False,
            "license_id": None,
            "source": None,
            "active_users": 3,
            "seat_limit": None,
            "grace_ends_at": None,
            "restricted": False,
            "hosts": [],
            "host_mismatch": False,
            "revoked_at": None,
        }

    def test_invalid_key_reports_invalid(self, app: FastAPI, client: TestClient) -> None:
        app.dependency_overrides[get_settings] = lambda: make_settings(license_key="garbage")

        body = client.get("/api/v1/license").json()
        assert body["status"] == "invalid"
        assert body["tier"] == "community"
        assert body["license_id"] is None

    def test_valid_key_reports_details(
        self, app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        private_key, public_key = generate_keypair()
        key = issue(
            private_key,
            {
                "v": 1,
                "kid": "vtest",
                "lic": "22222222-2222-2222-2222-222222222222",
                "tier": "enterprise",
                "licensee": "Acme Oy",
                "seats": 25,
                "issued_at": "2020-01-01",
                "expires_at": "2099-01-01",
                "features": ["sso", "audit-log"],
            },
        )
        app.dependency_overrides[get_settings] = lambda: make_settings(license_key=key)
        monkeypatch.setitem(license_mod.VENDOR_PUBLIC_KEYS, "vtest", public_key)

        body = client.get("/api/v1/license").json()

        assert body == {
            "status": "valid",
            "tier": "enterprise",
            "licensee": "Acme Oy",
            "seats": 25,
            "expires_at": "2099-01-01",
            # "audit-log" is not a known feature id and is ignored.
            "features": ["sso"],
            "business_features": list(BUSINESS_FEATURES),
            "owner_only": False,
            "trial_used": False,
            "license_id": "22222222-2222-2222-2222-222222222222",
            "source": "env",
            "active_users": 3,
            "seat_limit": 27,
            "grace_ends_at": "2099-01-31",
            "restricted": False,
            "hosts": [],
            "host_mismatch": False,
            "revoked_at": None,
        }

    def test_expired_key_reports_details_with_expired_status(
        self, app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        private_key, public_key = generate_keypair()
        key = issue(
            private_key,
            {
                "v": 1,
                "kid": "vtest2",
                "lic": "33333333-3333-3333-3333-333333333333",
                "tier": "commercial",
                "licensee": "Acme Oy",
                "seats": 5,
                "issued_at": "2020-01-01",
                "expires_at": "2020-06-01",
                "features": [],
            },
        )
        app.dependency_overrides[get_settings] = lambda: make_settings(license_key=key)
        monkeypatch.setitem(license_mod.VENDOR_PUBLIC_KEYS, "vtest2", public_key)

        body = client.get("/api/v1/license").json()

        assert body["status"] == "expired"
        # A key signed with the old "commercial" tier reads as "team" (alias).
        assert body["tier"] == "team"
        assert body["expires_at"] == "2020-06-01"
        assert body["grace_ends_at"] == "2020-07-01"
        assert body["restricted"] is True


class TestSeatLimitField:
    def test_community_is_three_once_the_build_has_vendor_keys(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _, public_key = generate_keypair()
        monkeypatch.setitem(license_mod.VENDOR_PUBLIC_KEYS, "vseatfield", public_key)

        body = client.get("/api/v1/license").json()

        assert body["tier"] == "community"
        assert body["seat_limit"] == 3
