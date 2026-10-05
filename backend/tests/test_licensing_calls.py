"""Licence refresh (LIC-27) and heartbeat (LIC-6): the services, the worker
tick and `/license/refresh`, against an `httpx.MockTransport` licence server
and in-memory SQLite.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser, get_current_user
from app.api.v1.license import _vendor_client
from app.core.config import Settings, get_settings
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import AuditEvent, Connector, LicenseState, Organization, User
from app.services.licensing import heartbeat, refresh, vendor
from app.services.licensing import keys as keys_mod
from app.services.licensing.issue import generate_keypair, issue, sign_token
from app.services.licensing.revocation import PREFIX as REVOCATION_PREFIX
from app.services.licensing.state import KeySource, effective_license, get_state, store_key
from app.worker.licensing import licence_calls

NOW = datetime(2026, 9, 25, 8, 0, tzinfo=UTC)
SERVER = "https://licensing.example.test"


@compiles(CITEXT, "sqlite")  # pragma: no cover - exercised implicitly by every test
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, AuditEvent.__table__),
    cast(Table, Connector.__table__),
    cast(Table, LicenseState.__table__),
]


class Signer:
    def __init__(self) -> None:
        self.private, self.public = generate_keypair()

    def key(self, *, expires_at: str, hosts: list[str] | None = None) -> str:
        payload: dict[str, object] = {
            "v": 1,
            "kid": "vcalls",
            "lic": "lic-calls",
            "tier": "commercial",
            "licensee": "Acme Oy",
            "seats": 5,
            "issued_at": "2026-01-01",
            "expires_at": expires_at,
            "features": [],
        }
        if hosts is not None:
            payload["hosts"] = hosts
        return issue(self.private, payload)

    def revocations(self, at: str, *, issued_at: str) -> str:
        return sign_token(
            self.private,
            {
                "v": 1,
                "kid": "vcalls",
                "issued_at": issued_at,
                "revoked": [{"lic": "lic-calls", "at": at}],
            },
            prefix=REVOCATION_PREFIX,
        )


@pytest.fixture
def signer(monkeypatch: pytest.MonkeyPatch) -> Signer:
    signer = Signer()
    monkeypatch.setitem(keys_mod.VENDOR_PUBLIC_KEYS, "vcalls", signer.public)
    return signer


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://t:t@localhost/t",
        "secret_key": "test-key",
        "install_id": "acme-prod",
        "license_server_url": SERVER + "/",
    }
    base.update(overrides)
    return Settings(**base)


class FakeServer:
    """Records requests; answers with `respond(request)`."""

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)

    def client(self) -> httpx.AsyncClient:
        return vendor.open_client(httpx.MockTransport(self))

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests if r.url.path == path]


def answering(body: dict[str, Any]) -> FakeServer:
    return FakeServer(lambda _request: httpx.Response(200, json=body))


def answering_key(key: str | None) -> FakeServer:
    return answering({"key": key})


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def org(sessionmaker: async_sessionmaker[AsyncSession]) -> Organization:
    async with sessionmaker() as session:
        organization = Organization(name="Acme", slug="acme")
        session.add(organization)
        await session.flush()
        session.add(
            User(
                organization_id=organization.id,
                email="anna@acme.com",
                display_name="anna",
                last_seen_at=NOW - timedelta(days=1),
            )
        )
        await session.commit()
        return organization


async def _state(sessionmaker: async_sessionmaker[AsyncSession]) -> LicenseState:
    async with sessionmaker() as session:
        state = await get_state(session)
    assert state is not None
    return state


# --- Scheduling ---------------------------------------------------------------------


def test_due_after_the_interval_and_at_most_hourly() -> None:
    day = timedelta(days=1)
    assert heartbeat.is_due(None, None, day, NOW)
    assert not heartbeat.is_due(NOW - timedelta(hours=2), NOW - timedelta(hours=2), day, NOW)
    assert heartbeat.is_due(NOW - timedelta(days=2), NOW - timedelta(days=2), day, NOW)
    # A failure 30 minutes ago waits; one two hours ago is retried.
    assert not heartbeat.is_due(NOW - timedelta(minutes=30), None, day, NOW)
    assert heartbeat.is_due(NOW - timedelta(hours=2), NOW - timedelta(days=3), day, NOW)


# --- Refresh ------------------------------------------------------------------------


@pytest.mark.usefixtures("org")
async def test_refresh_stores_a_renewed_key_and_reports_the_sign_in_host(
    sessionmaker: async_sessionmaker[AsyncSession], signer: Signer
) -> None:
    current = signer.key(expires_at="2026-10-31")
    renewed = signer.key(expires_at="2027-10-31", hosts=["annotate.acme.com"])
    server = answering_key(renewed)
    async with sessionmaker() as session:
        await store_key(session, current, source=KeySource.ADMIN)
        # A sign-in on the real host is what the refresh reports.
        await effective_license(
            session, make_settings(), now=NOW, host="annotate.acme.com", record=True
        )
        await session.commit()

    async with sessionmaker() as session, server.client() as client:
        await refresh.refresh(session, make_settings(), client, now=NOW)
        await session.commit()

    assert server.bodies("/v1/refresh") == [
        {
            "license_id": "lic-calls",
            "install_id": "acme-prod",
            "host": "annotate.acme.com",
            "active_users": 1,
            "version": heartbeat.VERSION,
        }
    ]
    state = await _state(sessionmaker)
    assert (state.key, state.key_source) == (renewed, "refresh")
    assert state.refresh_succeeded_at is not None
    assert state.refresh_error is None
    assert state.refresh_payload == server.bodies("/v1/refresh")[0]


@pytest.mark.usefixtures("org")
async def test_refresh_with_nothing_newer_succeeds_without_a_change(
    sessionmaker: async_sessionmaker[AsyncSession], signer: Signer
) -> None:
    current = signer.key(expires_at="2027-10-31")
    older = signer.key(expires_at="2027-01-31")
    for answer in (None, older):
        async with sessionmaker() as session, answering_key(answer).client() as client:
            await store_key(session, current, source=KeySource.ADMIN)
            await refresh.refresh(session, make_settings(), client, now=NOW)
            await session.commit()
        state = await _state(sessionmaker)
        assert (state.key, state.key_source) == (current, "admin")
        assert state.refresh_error is None


@pytest.mark.usefixtures("org")
@pytest.mark.parametrize(
    ("respond", "error"),
    [
        (lambda _r: httpx.Response(503), "HTTP 503"),
        (lambda _r: httpx.Response(200, text="<html>"), "not JSON"),
        (lambda _r: httpx.Response(200, json={"key": 42}), "no usable key"),
        (lambda _r: httpx.Response(200, json={"key": "ANN1.forged.key"}), "not valid"),
    ],
)
async def test_a_failed_refresh_is_recorded_and_changes_nothing(
    sessionmaker: async_sessionmaker[AsyncSession],
    signer: Signer,
    respond: Callable[[httpx.Request], httpx.Response],
    error: str,
) -> None:
    key = signer.key(expires_at="2027-10-31")
    async with sessionmaker() as session, FakeServer(respond).client() as client:
        await store_key(session, key, source=KeySource.ADMIN)
        await refresh.refresh(session, make_settings(), client, now=NOW)
        await session.commit()

    state = await _state(sessionmaker)
    assert state.key == key
    assert state.refresh_attempted_at is not None
    assert state.refresh_succeeded_at is None
    assert error in (state.refresh_error or "")


async def test_a_key_for_another_host_is_refused(
    sessionmaker: async_sessionmaker[AsyncSession], signer: Signer
) -> None:
    settings = make_settings(
        license_key=signer.key(expires_at="2026-10-31"), public_hostname="annotate.acme.com"
    )
    wrong_host = signer.key(expires_at="2027-10-31", hosts=["other.example.org"])
    async with sessionmaker() as session, answering_key(wrong_host).client() as client:
        state = await refresh.refresh(session, settings, client, now=NOW)
        await session.commit()
    assert state.key is None
    assert "not valid for this installation" in (state.refresh_error or "")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"license_refresh_enabled": False}, "switched off"),
        ({"license_server_url": None}, "No licence server"),
        ({"license_key": None}, "nothing to refresh"),
    ],
)
async def test_refresh_says_why_it_cannot_run(
    sessionmaker: async_sessionmaker[AsyncSession],
    signer: Signer,
    overrides: dict[str, Any],
    reason: str,
) -> None:
    settings = make_settings(**{"license_key": signer.key(expires_at="2027-10-31"), **overrides})
    server = answering_key(None)
    async with sessionmaker() as session, server.client() as client:
        with pytest.raises(refresh.RefreshUnavailableError, match=reason):
            await refresh.refresh(session, settings, client, now=NOW)
        assert not await refresh.refresh_if_due(session, settings, client, now=NOW)
    assert server.requests == []


async def test_refresh_if_due_runs_daily_and_retries_hourly(
    sessionmaker: async_sessionmaker[AsyncSession], signer: Signer
) -> None:
    settings = make_settings(license_key=signer.key(expires_at="2027-10-31"))
    down = True
    server = FakeServer(
        lambda _r: httpx.Response(503) if down else httpx.Response(200, json={"key": None})
    )

    async def tick(at: datetime) -> bool:
        async with sessionmaker() as session, server.client() as client:
            ran = await refresh.refresh_if_due(session, settings, client, now=at)
            await session.commit()
        return ran

    assert await tick(NOW)  # fails
    assert not await tick(NOW + timedelta(minutes=30))
    down = False
    assert await tick(NOW + timedelta(hours=1))  # retried, succeeds
    assert not await tick(NOW + timedelta(hours=20))
    assert await tick(NOW + timedelta(hours=25))
    assert len(server.requests) == 3


# --- Heartbeat ------------------------------------------------------------------------


@pytest.mark.usefixtures("org")
async def test_heartbeat_sends_the_preview_payload_weekly(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    settings = make_settings(telemetry_enabled=True, license_fingerprint_salt="salt")
    server = FakeServer(lambda _r: httpx.Response(204))

    async def tick(at: datetime) -> bool:
        async with sessionmaker() as session, server.client() as client:
            sent = await heartbeat.send_if_due(session, settings, client, now=at)
            await session.commit()
        return sent

    assert await tick(NOW)
    assert not await tick(NOW + timedelta(days=6))
    assert await tick(NOW + timedelta(days=7))

    body = server.bodies("/v1/heartbeat")[0]
    assert set(body) == {"install_id", "version", "licence_type", "active_users", "fingerprint"}
    assert body["licence_type"] == "community"
    assert "acme.com" not in json.dumps(body)  # hashed, never plain
    state = await _state(sessionmaker)
    assert state.heartbeat_sent_at is not None
    assert state.heartbeat_payload == server.bodies("/v1/heartbeat")[-1]


async def test_heartbeat_is_on_by_default_and_can_be_turned_off(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    assert make_settings().telemetry_enabled
    server = FakeServer(lambda _r: httpx.Response(204))
    settings = make_settings(telemetry_enabled=False)
    async with sessionmaker() as session, server.client() as client:
        assert not await heartbeat.send_if_due(session, settings, client, now=NOW)
    assert server.requests == []


@pytest.mark.usefixtures("org")
async def test_a_failed_heartbeat_is_recorded(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with sessionmaker() as session, FakeServer(unreachable).client() as client:
        await heartbeat.send_if_due(session, make_settings(telemetry_enabled=True), client, now=NOW)
        await session.commit()
    state = await _state(sessionmaker)
    assert state.heartbeat_sent_at is None
    assert state.heartbeat_error == "Could not reach the licence server (ConnectError)."


# --- Worker tick -------------------------------------------------------------------------


@pytest.mark.usefixtures("org")
async def test_worker_tick_runs_both(
    sessionmaker: async_sessionmaker[AsyncSession], signer: Signer
) -> None:
    settings = make_settings(
        license_key=signer.key(expires_at="2027-10-31"), telemetry_enabled=True
    )
    server = FakeServer(lambda _r: httpx.Response(200, json={"key": None}))
    ctx: dict[str, Any] = {
        "settings": settings,
        "sessionmaker": sessionmaker,
        "licence_transport": httpx.MockTransport(server),
    }
    assert await licence_calls(ctx) == {"refreshed": True, "heartbeat": True}
    assert await licence_calls(ctx) == {"refreshed": False, "heartbeat": False}
    assert await licence_calls({**ctx, "settings": make_settings(license_server_url=None)}) == {
        "refreshed": False,
        "heartbeat": False,
    }


# --- API ----------------------------------------------------------------------------------


@pytest.fixture
def app(
    sessionmaker: async_sessionmaker[AsyncSession], org: Organization, signer: Signer
) -> Iterator[FastAPI]:
    application: FastAPI = create_app()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_session] = _get_session
    application.dependency_overrides[get_current_user] = lambda: CurrentUser(
        id=uuid.uuid4(),
        organization_id=org.id,
        email="admin@acme.com",
        is_superuser=True,
        is_service=False,
        scopes=frozenset(),
    )
    application.dependency_overrides[get_settings] = lambda: make_settings(
        license_key=signer.key(expires_at="2027-10-31")
    )
    yield application
    application.dependency_overrides.clear()


def _serve(app: FastAPI, server: FakeServer) -> None:
    async def client() -> AsyncIterator[httpx.AsyncClient]:
        async with server.client() as c:
            yield c

    app.dependency_overrides[_vendor_client] = client


def test_get_shows_what_would_be_sent(app: FastAPI) -> None:
    body = TestClient(app).get("/api/v1/license/refresh").json()
    assert body["enabled"] is True
    assert body["server_configured"] is True
    assert body["payload"]["license_id"] == "lic-calls"
    assert body["payload"]["host"] is None
    assert body["attempted_at"] is None


async def test_post_refreshes_now_and_is_audited(
    app: FastAPI, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    server = answering_key(None)
    _serve(app, server)
    response = TestClient(app).post("/api/v1/license/refresh")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["succeeded_at"] is not None
    assert body["last_payload"]["license_id"] == "lic-calls"
    assert len(server.requests) == 1
    async with sessionmaker() as session:
        actions = (await session.scalars(select(AuditEvent.action))).all()
    assert list(actions) == ["license.refresh"]


def test_post_without_a_server_is_a_conflict(app: FastAPI, signer: Signer) -> None:
    app.dependency_overrides[get_settings] = lambda: make_settings(
        license_key=signer.key(expires_at="2027-10-31"), license_server_url=None
    )
    response = TestClient(app).post("/api/v1/license/refresh")
    assert response.status_code == 409
    assert "No licence server" in response.json()["detail"]


# --- Revocation lists by refresh (LIC-8) ------------------------------------------


async def test_refresh_stores_a_newer_revocation_list_and_the_licence_expires(
    sessionmaker: async_sessionmaker[AsyncSession], signer: Signer
) -> None:
    settings = make_settings(license_key=signer.key(expires_at="2027-10-31"))
    newer = signer.revocations("2026-09-20", issued_at="2026-09-21")
    older = signer.revocations("2026-01-01", issued_at="2026-08-01")
    for answer in (newer, older):
        async with sessionmaker() as session, answering({"revocations": answer}).client() as client:
            await refresh.refresh(session, settings, client, now=NOW)
            await session.commit()

    state = await _state(sessionmaker)
    assert state.revocations == newer
    assert state.refresh_error is None
    async with sessionmaker() as session:
        licence = await effective_license(session, settings, now=NOW)
    assert licence.revoked_at == datetime(2026, 9, 20).date()
    assert licence.status.value == "expired"


async def test_a_forged_revocation_list_is_an_error(
    sessionmaker: async_sessionmaker[AsyncSession], signer: Signer
) -> None:
    settings = make_settings(license_key=signer.key(expires_at="2027-10-31"))
    forged = Signer().revocations("2026-09-01", issued_at="2026-09-21")
    server = FakeServer(lambda _r: httpx.Response(200, json={"revocations": forged}))
    async with sessionmaker() as session, server.client() as client:
        state = await refresh.refresh(session, settings, client, now=NOW)
        await session.commit()
    assert state.revocations is None
    assert "does not verify" in (state.refresh_error or "")


async def test_a_revoked_key_from_the_server_is_not_stored(
    sessionmaker: async_sessionmaker[AsyncSession], signer: Signer
) -> None:
    settings = make_settings(license_key=signer.key(expires_at="2026-12-31"))
    answer = {
        "key": signer.key(expires_at="2027-12-31"),
        "revocations": signer.revocations("2026-09-01", issued_at="2026-09-21"),
    }
    server = FakeServer(lambda _r: httpx.Response(200, json=answer))
    async with sessionmaker() as session, server.client() as client:
        state = await refresh.refresh(session, settings, client, now=NOW)
        await session.commit()
    assert state.key is None
    assert "not valid for this installation" in (state.refresh_error or "")


async def test_put_refuses_a_revoked_key(
    app: FastAPI, sessionmaker: async_sessionmaker[AsyncSession], signer: Signer
) -> None:
    async with sessionmaker() as session:
        state = await get_state(session, create=True)
        assert state is not None
        state.revocations = signer.revocations("2026-01-31", issued_at="2026-02-01")
        await session.commit()

    response = TestClient(app).put(
        "/api/v1/license", json={"key": signer.key(expires_at="2099-01-01")}
    )
    assert response.status_code == 422
    assert "revoked on 2026-01-31" in response.json()["detail"]

    body = TestClient(app).get("/api/v1/license").json()
    assert body["revoked_at"] == "2026-01-31"
    assert body["status"] == "expired"


def test_empty_licensing_env_values_mean_unset() -> None:
    # docker compose passes `${APP_INSTALL_ID:-}` as an empty string.
    settings = make_settings(install_id="", license_server_url="  ", license_key="")
    assert settings.install_id is None
    assert settings.license_server_url is None
    assert settings.license_key is None
    assert make_settings(install_id="acme-prod").install_id == "acme-prod"
