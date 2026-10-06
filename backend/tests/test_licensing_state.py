"""Tests for the licence in force and the clock guard (LIC-25, LIC-26).

`services/licensing/state.py` — the pure `resolve` and the database-backed
`effective_license` — plus `PUT` / `DELETE /license`, on in-memory SQLite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from datetime import UTC, date, datetime
from typing import cast
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser, get_current_user
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import AuditEvent, LicenseState, Organization, User
from app.services.licensing import license as license_mod
from app.services.licensing.issue import generate_keypair, issue
from app.services.licensing.license import LicenseStatus
from app.services.licensing.state import (
    EffectiveLicense,
    KeySource,
    clear_key,
    effective_license,
    get_state,
    grace_ends,
    is_restricted,
    resolve,
    store_key,
)

TODAY = date(2026, 9, 25)


@compiles(CITEXT, "sqlite")  # pragma: no cover - exercised implicitly by every test
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, AuditEvent.__table__),
    cast(Table, LicenseState.__table__),
]


class Keys:
    def __init__(self) -> None:
        self.private, public = generate_keypair()
        self.public: Mapping[str, bytes] = {"vstate": public}

    def licence(
        self,
        seats: int = 5,
        *,
        expires_at: str = "2099-01-01",
        lic: str = "a",
        hosts: list[str] | None = None,
    ) -> str:
        payload: dict[str, object] = {
            "v": 1,
            "kid": "vstate",
            "lic": lic,
            "tier": "commercial",
            "licensee": "Acme Oy",
            "seats": seats,
            "issued_at": "2020-01-01",
            "expires_at": expires_at,
            "features": [],
        }
        if hosts is not None:
            payload["hosts"] = hosts
        return issue(self.private, payload)


@pytest.fixture(scope="module")
def keys() -> Keys:
    return Keys()


def make_settings(license_key: str | None = None) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://t:t@localhost/t",
        secret_key="test-key",
        license_key=license_key,
    )


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


# --- resolve ------------------------------------------------------------------------


class TestResolve:
    def test_no_key_is_community(self, keys: Keys) -> None:
        licence = resolve([(KeySource.ENV, None)], today=TODAY, public_keys=keys.public)
        assert licence.status is LicenseStatus.COMMUNITY
        assert licence.source is None
        assert licence.enforcing

    def test_garbage_is_invalid(self, keys: Keys) -> None:
        licence = resolve([(KeySource.ENV, "garbage")], today=TODAY, public_keys=keys.public)
        assert licence.status is LicenseStatus.INVALID
        assert not licence.keyed

    def test_without_vendor_keys_nothing_is_enforced(self, keys: Keys) -> None:
        licence = resolve([(KeySource.ENV, keys.licence())], today=TODAY, public_keys={})
        assert licence.status is LicenseStatus.INVALID
        assert not licence.enforcing

    def test_the_valid_key_expiring_last_wins(self, keys: Keys) -> None:
        env = keys.licence(5, expires_at="2027-01-01", lic="env")
        admin = keys.licence(8, expires_at="2028-01-01", lic="admin")

        licence = resolve(
            [(KeySource.ENV, env), (KeySource.ADMIN, admin)], today=TODAY, public_keys=keys.public
        )

        assert licence.source is KeySource.ADMIN
        assert licence.license is not None
        assert licence.license.seats == 8

    def test_a_valid_key_beats_a_later_expired_one(self, keys: Keys) -> None:
        # Impossible by dates alone, so model it with an expired key and a valid one.
        expired = keys.licence(50, expires_at="2026-01-01", lic="old")
        valid = keys.licence(5, expires_at="2026-12-31", lic="new")

        licence = resolve(
            [(KeySource.ENV, expired), (KeySource.ADMIN, valid)],
            today=TODAY,
            public_keys=keys.public,
        )

        assert licence.status is LicenseStatus.VALID
        assert licence.source is KeySource.ADMIN

    def test_an_expired_key_is_still_the_licence_in_force(self, keys: Keys) -> None:
        expired = keys.licence(5, expires_at="2026-01-01")
        licence = resolve(
            [(KeySource.ENV, expired), (KeySource.ADMIN, "garbage")],
            today=TODAY,
            public_keys=keys.public,
        )
        assert licence.status is LicenseStatus.EXPIRED
        assert licence.keyed

    def test_seats_never_add_up_across_keys(self, keys: Keys) -> None:
        licence = resolve(
            [
                (KeySource.ENV, keys.licence(5, lic="x")),
                (KeySource.ADMIN, keys.licence(5, lic="y")),
            ],
            today=TODAY,
            public_keys=keys.public,
        )
        assert licence.license is not None
        assert licence.license.seats == 5


# --- effective_license and the clock guard ------------------------------------------


async def test_database_key_is_read_alongside_the_env_key(
    sessionmaker: async_sessionmaker[AsyncSession], keys: Keys, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(license_mod.VENDOR_PUBLIC_KEYS, "vstate", keys.public["vstate"])
    async with sessionmaker() as session:
        await store_key(session, keys.licence(9), source=KeySource.REFRESH)
        await session.commit()

    async with sessionmaker() as session:
        licence = await effective_license(session, make_settings())

    assert licence.source is KeySource.REFRESH
    assert licence.license is not None
    assert licence.license.seats == 9


async def test_sign_in_records_the_date_and_a_read_does_not(
    sessionmaker: async_sessionmaker[AsyncSession], keys: Keys
) -> None:
    now = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
    async with sessionmaker() as session:
        await effective_license(session, make_settings(), now=now, public_keys=keys.public)
        await session.commit()
        assert await get_state(session) is None

        await effective_license(
            session, make_settings(), now=now, record=True, public_keys=keys.public
        )
        await session.commit()
        state = await get_state(session)
        assert state is not None
        assert state.clock_high_water == now.date()


async def test_setting_the_clock_back_does_not_revive_an_expired_key(
    sessionmaker: async_sessionmaker[AsyncSession], keys: Keys
) -> None:
    key = keys.licence(expires_at="2026-09-30")
    settings = make_settings(key)
    async with sessionmaker() as session:
        after_expiry = datetime(2026, 10, 5, tzinfo=UTC)
        licence = await effective_license(
            session, settings, now=after_expiry, record=True, public_keys=keys.public
        )
        await session.commit()
        assert licence.status is LicenseStatus.EXPIRED

        wound_back = datetime(2026, 9, 1, tzinfo=UTC)
        licence = await effective_license(
            session, settings, now=wound_back, record=True, public_keys=keys.public
        )
        await session.commit()

    assert licence.status is LicenseStatus.EXPIRED
    assert licence.today == date(2026, 10, 5)


async def test_high_water_never_moves_backwards(
    sessionmaker: async_sessionmaker[AsyncSession], keys: Keys
) -> None:
    async with sessionmaker() as session:
        for day in (10, 20, 15):
            now = datetime(2026, 9, day, tzinfo=UTC)
            await effective_license(
                session, make_settings(), now=now, record=True, public_keys=keys.public
            )
            await session.commit()
        state = await get_state(session)
    assert state is not None
    assert state.clock_high_water == date(2026, 9, 20)


async def test_clear_key_only_reports_a_change_when_there_was_one(
    sessionmaker: async_sessionmaker[AsyncSession], keys: Keys
) -> None:
    async with sessionmaker() as session:
        assert not await clear_key(session)
        await store_key(session, keys.licence(), source=KeySource.ADMIN)
        assert await clear_key(session)
        state = await get_state(session)
    assert state is not None
    assert state.key is None
    assert state.key_source is None


# --- PUT / DELETE /license ----------------------------------------------------------


@pytest.fixture
async def org(sessionmaker: async_sessionmaker[AsyncSession]) -> Organization:
    async with sessionmaker() as session:
        organization = Organization(name="Acme", slug="acme")
        session.add(organization)
        await session.commit()
        return organization


@pytest.fixture
def client(
    sessionmaker: async_sessionmaker[AsyncSession],
    org: Organization,
    keys: Keys,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setitem(license_mod.VENDOR_PUBLIC_KEYS, "vstate", keys.public["vstate"])
    application: FastAPI = create_app()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    admin = CurrentUser(
        id=uuid4(),
        organization_id=org.id,
        email="admin@acme.com",
        is_superuser=True,
        is_service=False,
        scopes=frozenset(),
    )
    application.dependency_overrides[get_session] = _get_session
    application.dependency_overrides[get_current_user] = lambda: admin
    application.dependency_overrides[get_settings] = lambda: make_settings()
    yield TestClient(application, raise_server_exceptions=False)
    application.dependency_overrides.clear()


async def _audit_actions(sessionmaker: async_sessionmaker[AsyncSession]) -> list[str]:
    async with sessionmaker() as session:
        return list((await session.scalars(select(AuditEvent.action))).all())


async def test_put_installs_a_valid_key(
    client: TestClient, keys: Keys, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    response = client.put("/api/v1/license", json={"key": f"  {keys.licence(12)}\n"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "valid"
    assert body["source"] == "admin"
    assert body["seats"] == 12
    assert body["seat_limit"] == 13
    assert "key" not in body
    assert await _audit_actions(sessionmaker) == ["license.update"]


def test_put_refuses_garbage(client: TestClient) -> None:
    response = client.put("/api/v1/license", json={"key": "ANN1.nope.nope"})

    assert response.status_code == 422
    assert response.json()["type"].endswith(":invalid-license-key")


def test_put_refuses_an_expired_key(client: TestClient, keys: Keys) -> None:
    response = client.put("/api/v1/license", json={"key": keys.licence(expires_at="2020-01-01")})

    assert response.status_code == 422
    assert "expired on 2020-01-01" in response.json()["detail"]


def test_put_requires_a_superuser(client: TestClient, keys: Keys, org: Organization) -> None:
    app = cast(FastAPI, client.app)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=uuid4(),
        organization_id=org.id,
        email="anna@acme.com",
        is_superuser=False,
        is_service=False,
        scopes=frozenset(),
    )
    assert client.put("/api/v1/license", json={"key": keys.licence()}).status_code == 403


async def test_delete_forgets_the_pasted_key(
    client: TestClient, keys: Keys, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    client.put("/api/v1/license", json={"key": keys.licence()})

    body = client.delete("/api/v1/license").json()

    assert body["status"] == "community"
    assert body["source"] is None
    assert body["seat_limit"] == 3
    assert await _audit_actions(sessionmaker) == ["license.update", "license.delete"]


async def test_delete_without_a_key_changes_nothing(
    client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    assert client.delete("/api/v1/license").status_code == 200
    assert await _audit_actions(sessionmaker) == []


async def test_without_vendor_keys_the_database_is_not_read(
    sessionmaker: async_sessionmaker[AsyncSession], keys: Keys
) -> None:
    async with sessionmaker() as session:
        await store_key(session, keys.licence(), source=KeySource.ADMIN)
        await session.commit()
        licence = await effective_license(session, make_settings(), record=True, public_keys={})

    assert licence.status is LicenseStatus.COMMUNITY
    assert not licence.enforcing


# --- restricted mode (LIC-5) --------------------------------------------------------


class TestRestriction:
    def _licence(self, keys: Keys, today: date) -> EffectiveLicense:
        key = keys.licence(expires_at="2026-06-30")
        return resolve([(KeySource.ENV, key)], today=today, public_keys=keys.public)

    def test_grace_runs_thirty_days_past_expiry(self, keys: Keys) -> None:
        assert grace_ends(self._licence(keys, TODAY)) == date(2026, 7, 30)

    def test_not_restricted_before_expiry_or_during_grace(self, keys: Keys) -> None:
        assert not is_restricted(self._licence(keys, date(2026, 6, 30)))
        assert not is_restricted(self._licence(keys, date(2026, 7, 30)))

    def test_restricted_the_day_after_grace(self, keys: Keys) -> None:
        assert is_restricted(self._licence(keys, date(2026, 7, 31)))

    def test_community_and_invalid_are_never_restricted(self, keys: Keys) -> None:
        for key in (None, "garbage"):
            licence = resolve([(KeySource.ENV, key)], today=TODAY, public_keys=keys.public)
            assert grace_ends(licence) is None
            assert not is_restricted(licence)


# --- Host binding (LIC-29) ---------------------------------------------------


async def test_sign_in_on_another_host_starts_the_grace_clock_once(
    sessionmaker: async_sessionmaker[AsyncSession], keys: Keys
) -> None:
    settings = make_settings(keys.licence(hosts=["annotate.acme.com"]))
    first = datetime(2026, 9, 25, tzinfo=UTC)
    async with sessionmaker() as session:
        licence = await effective_license(
            session, settings, now=first, host="copy.example.org", public_keys=keys.public
        )
        await session.commit()
        assert licence.status is LicenseStatus.EXPIRED
        assert await get_state(session) is None  # a read writes nothing

        await effective_license(
            session,
            settings,
            now=first,
            host="copy.example.org",
            record=True,
            public_keys=keys.public,
        )
        await session.commit()

        later = datetime(2026, 11, 1, tzinfo=UTC)
        licence = await effective_license(
            session,
            settings,
            now=later,
            host="copy.example.org",
            record=True,
            public_keys=keys.public,
        )
        await session.commit()
        state = await get_state(session)

    assert state is not None
    assert state.host_mismatch_since == first.date()
    assert grace_ends(licence) == date(2026, 10, 25)
    assert is_restricted(licence)


async def test_loopback_sign_in_keeps_the_clock_and_a_bound_host_clears_it(
    sessionmaker: async_sessionmaker[AsyncSession], keys: Keys
) -> None:
    settings = make_settings(keys.licence(hosts=["*.acme.com"]))
    now = datetime(2026, 9, 25, tzinfo=UTC)

    async def sign_in(session: AsyncSession, host: str) -> None:
        await effective_license(
            session, settings, now=now, host=host, record=True, public_keys=keys.public
        )
        await session.commit()

    async with sessionmaker() as session:
        await sign_in(session, "annotate.other.com")
        await sign_in(session, "localhost")
        state = await get_state(session)
        assert state is not None
        assert state.host_mismatch_since == now.date()

        await sign_in(session, "annotate.acme.com")
        await session.refresh(state)
        assert state.host_mismatch_since is None


async def test_put_refuses_a_key_bound_to_other_hosts(
    client: TestClient, keys: Keys, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    # TestClient's host is `testserver`.
    response = client.put(
        "/api/v1/license", json={"key": keys.licence(hosts=["annotate.acme.com"])}
    )

    assert response.status_code == 422
    assert "annotate.acme.com, not testserver" in response.json()["detail"]
    assert await _audit_actions(sessionmaker) == []


def test_put_and_get_report_the_binding(client: TestClient, keys: Keys) -> None:
    key = keys.licence(hosts=["testserver", "*.acme.com"])
    body = client.put("/api/v1/license", json={"key": key}).json()
    assert body["hosts"] == ["testserver", "*.acme.com"]
    assert body["host_mismatch"] is False

    moved = client.get("/api/v1/license", headers={"Host": "annotate.other.com"}).json()
    assert moved["status"] == "expired"
    assert moved["host_mismatch"] is True
    assert moved["restricted"] is False
