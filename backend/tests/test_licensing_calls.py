"""Licence refresh (LIC-27) and heartbeat (LIC-6): the services, the worker
tick and `/license/refresh`, against an `httpx.MockTransport` licence server
and in-memory SQLite.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
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


def test_due_after_the_interval_unless_a_retry_waits() -> None:
    day = timedelta(days=1)
    assert vendor.is_due(None, None, day, NOW)
    assert not vendor.is_due(None, NOW - timedelta(hours=2), day, NOW)
    assert vendor.is_due(None, NOW - timedelta(days=2), day, NOW)
    # A retry set for later waits; one whose time has come runs.
    assert not vendor.is_due(NOW + timedelta(minutes=30), None, day, NOW)
    assert vendor.is_due(NOW - timedelta(minutes=1), NOW - timedelta(days=3), day, NOW)


def test_ticks_exactly_an_interval_apart_are_due() -> None:
    """The 2-hour bug: an attempt stamped just after its tick skipped the next tick."""
    tick = datetime(2026, 10, 14, 13, 17, 10, tzinfo=UTC)
    stamped = tick + timedelta(milliseconds=350)
    retry_at = vendor.next_attempt(httpx.ConnectError("refused"), 1, stamped)
    assert retry_at == stamped + vendor.RETRY_AFTER
    assert vendor.is_due(retry_at, None, timedelta(days=1), tick + timedelta(hours=1))
    # The same for the interval after a success.
    assert vendor.is_due(None, stamped, timedelta(days=1), tick + timedelta(days=1))
    assert not vendor.is_due(None, stamped, timedelta(days=1), tick + timedelta(hours=23))


def _status_error(status: int, retry_after: str | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", SERVER + "/v1/refresh")
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    response = httpx.Response(status, headers=headers, request=request)
    return httpx.HTTPStatusError("failed", request=request, response=response)


def test_a_waking_server_is_retried_after_minutes_then_hourly() -> None:
    for exc in (_status_error(502), _status_error(503), _status_error(504), httpx.ReadTimeout("")):
        assert vendor.next_attempt(exc, 1, NOW) == NOW + vendor.QUICK_RETRY
    waking = _status_error(503)
    assert vendor.next_attempt(waking, vendor.QUICK_RETRIES, NOW) == NOW + vendor.QUICK_RETRY
    assert vendor.next_attempt(waking, vendor.QUICK_RETRIES + 1, NOW) == NOW + vendor.RETRY_AFTER
    # Anything else is not a cold start.
    for exc in (_status_error(500), _status_error(422), httpx.ConnectError("refused")):
        assert vendor.next_attempt(exc, 1, NOW) == NOW + vendor.RETRY_AFTER


def test_retry_after_is_honoured_within_reason() -> None:
    # Sooner than our own delay changes nothing; later is waited for.
    assert vendor.next_attempt(_status_error(503, "30"), 1, NOW) == NOW + vendor.QUICK_RETRY
    assert vendor.next_attempt(_status_error(503, "600"), 1, NOW) == NOW + timedelta(minutes=10)
    assert vendor.next_attempt(_status_error(429, "7200"), 1, NOW) == NOW + timedelta(hours=2)
    later = format_datetime(NOW + timedelta(hours=3), usegmt=True)
    assert vendor.next_attempt(_status_error(503, later), 1, NOW) == NOW + timedelta(hours=3)
    # Not believed beyond a day; nonsense is ignored.
    assert vendor.next_attempt(_status_error(503, "9999999"), 1, NOW) == NOW + timedelta(days=1)
    assert vendor.next_attempt(_status_error(503, "soon"), 1, NOW) == NOW + vendor.QUICK_RETRY


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
            # Proof of holding the key, not just its id (LIC-27).
            "key_digest": hashlib.sha256(current.encode()).hexdigest(),
            "install_id": "acme-prod",
            "host": "annotate.acme.com",
            "active_users": 1,
            "version": "dev",
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


async def test_refresh_if_due_runs_daily_and_retries_soon_after_a_cold_start(
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

    assert await tick(NOW)  # fails: the server is starting
    assert not await tick(NOW + timedelta(minutes=2))
    state = await _state(sessionmaker)
    # SQLite hands back naive UTC datetimes.
    retry_at = (NOW + vendor.QUICK_RETRY).replace(tzinfo=None)
    assert (state.refresh_failures, state.refresh_retry_at) == (1, retry_at)
    down = False
    assert await tick(NOW + timedelta(minutes=5))  # retried, succeeds
    state = await _state(sessionmaker)
    assert (state.refresh_failures, state.refresh_retry_at, state.refresh_error) == (0, None, None)
    assert not await tick(NOW + timedelta(hours=20))
    assert await tick(NOW + timedelta(hours=25))
    assert len(server.requests) == 3


async def test_quick_retries_stop_after_a_few(
    sessionmaker: async_sessionmaker[AsyncSession], signer: Signer
) -> None:
    settings = make_settings(license_key=signer.key(expires_at="2027-10-31"))
    server = FakeServer(lambda _r: httpx.Response(503))
    attempts: list[int] = []
    # The worker's cron: every five minutes, for two hours.
    for minutes in range(0, 120, 5):
        async with sessionmaker() as session, server.client() as client:
            if await refresh.refresh_if_due(
                session, settings, client, now=NOW + timedelta(minutes=minutes)
            ):
                attempts.append(minutes)
            await session.commit()
    # The first try, QUICK_RETRIES quick ones, then hourly.
    assert attempts == [0, 5, 10, 15, 75]


# --- A slow licence server, over a real socket ------------------------------------

SlowServer = Callable[[float | None], Awaitable[str]]


@pytest.fixture
async def slow_server() -> AsyncIterator[SlowServer]:
    """A licence server on localhost that answers after a delay, or never (``None``)."""
    servers: list[asyncio.Server] = []
    done = asyncio.Event()

    async def start(delay: float | None) -> str:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readuntil(b"\r\n\r\n")
            if delay is None:
                await done.wait()
            else:
                await asyncio.sleep(delay)
                writer.write(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        servers.append(server)
        port = server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    yield start
    done.set()
    for server in servers:
        server.close()
        await server.wait_closed()


@pytest.mark.usefixtures("org")
async def test_a_slow_server_is_waited_for_and_a_silent_one_retried_soon(
    sessionmaker: async_sessionmaker[AsyncSession],
    slow_server: SlowServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The worker's timeouts scaled down: a "cold start" of 0.3 s against 1 s.
    monkeypatch.setattr(vendor, "BACKGROUND_TIMEOUT", httpx.Timeout(1.0, connect=1.0))

    async def tick(url: str) -> dict[str, Any]:
        settings = make_settings(license_server_url=url, telemetry_enabled=True)
        return await licence_calls({"settings": settings, "sessionmaker": sessionmaker})

    assert (await tick(await slow_server(0.3)))["heartbeat"]
    state = await _state(sessionmaker)
    assert state.heartbeat_sent_at is not None
    assert state.heartbeat_error is None

    async with sessionmaker() as session:
        row = await get_state(session)
        assert row is not None
        row.heartbeat_sent_at = None  # due again
        await session.commit()
    assert (await tick(await slow_server(None)))["heartbeat"]
    state = await _state(sessionmaker)
    assert state.heartbeat_error == "The licence server did not answer in time."
    assert state.heartbeat_failures == 1
    assert state.heartbeat_attempted_at is not None
    assert state.heartbeat_retry_at == state.heartbeat_attempted_at + vendor.QUICK_RETRY


# --- Heartbeat ------------------------------------------------------------------------


@pytest.mark.usefixtures("org")
async def test_heartbeat_sends_the_preview_payload_weekly(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    settings = make_settings(
        telemetry_enabled=True, license_fingerprint_salt="salt", version="0.1.3"
    )
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
    assert body["version"] == "0.1.3"  # APP_VERSION, baked in by the release
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
