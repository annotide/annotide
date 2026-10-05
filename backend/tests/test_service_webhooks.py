"""`services/webhooks.py` (API-4): signing, matching, delivery and retry."""

from __future__ import annotations

import asyncio
import json
import re
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import httpcore
import httpx
import pytest
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.netguard import _PublicOnlyBackend
from app.db.base import Base
from app.models import (
    Organization,
    Project,
    Webhook,
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookFormat,
)
from app.services.webhooks import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    backoff_seconds,
    deliver_due,
    emit_event,
    generate_secret,
    public_destination,
    public_only_transport,
    render_body,
    seal_secret,
    sign,
    signing_secret,
    verify,
)

_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, Project.__table__),
    cast(Table, Webhook.__table__),
    cast(Table, WebhookDelivery.__table__),
]


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


async def _seed(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID, UUID]:
    """Organisation with two projects. Returns (org_id, project_a, project_b)."""
    async with sessionmaker() as session:
        org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:6]}")
        session.add(org)
        await session.flush()
        a = Project(organization_id=org.id, name="a", settings={}, workflow={})
        b = Project(organization_id=org.id, name="b", settings={}, workflow={})
        session.add_all([a, b])
        await session.commit()
        return org.id, a.id, b.id


async def _hook(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    org_id: UUID,
    project_id: UUID | None,
    events: list[str],
    url: str = "https://hooks.example/in",
    is_active: bool = True,
    hook_format: WebhookFormat = WebhookFormat.JSON,
) -> Webhook:
    async with sessionmaker() as session:
        hook = Webhook(
            organization_id=org_id,
            project_id=project_id,
            url=url,
            events=events,
            secret=seal_secret(generate_secret()),
            is_active=is_active,
            format=hook_format,
        )
        session.add(hook)
        await session.commit()
        await session.refresh(hook)
        return hook


async def _deliveries(sessionmaker: async_sessionmaker[AsyncSession]) -> list[WebhookDelivery]:
    async with sessionmaker() as session:
        return list(await session.scalars(select(WebhookDelivery)))


class TestSignature:
    def test_sign_and_verify_round_trip(self) -> None:
        secret = generate_secret()
        body = b'{"a":1}'
        header = sign(secret, int(datetime.now(UTC).timestamp()), body)
        assert header.startswith("t=") and ",v1=" in header
        assert verify(secret, header, body) is True

    def test_verify_rejects_wrong_secret_body_or_stale_timestamp(self) -> None:
        secret = generate_secret()
        body = b"{}"
        now = int(datetime.now(UTC).timestamp())
        header = sign(secret, now, body)
        assert verify("other", header, body) is False
        assert verify(secret, header, b"{ }") is False
        assert verify(secret, sign(secret, now - 3600, body), body) is False
        assert verify(secret, "garbage", body) is False
        assert verify(secret, f"t={now},v1=\u00e9\u20ac", body) is False

    def test_secrets_are_unique_and_long(self) -> None:
        a, b = generate_secret(), generate_secret()
        assert a != b
        assert len(a) == 64


class TestBackoff:
    def test_doubles_from_30s_and_caps(self) -> None:
        assert [backoff_seconds(n) for n in (1, 2, 3, 4)] == [30, 60, 120, 240]
        assert backoff_seconds(30) == 6 * 60 * 60


class TestEmitEvent:
    async def test_matches_scope_and_subscription(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, project_a, project_b = await _seed(sessionmaker)
        org_wide = await _hook(sessionmaker, org_id=org_id, project_id=None, events=["*"])
        only_a = await _hook(
            sessionmaker, org_id=org_id, project_id=project_a, events=["annotation.submitted"]
        )
        await _hook(sessionmaker, org_id=org_id, project_id=project_b, events=["*"])
        await _hook(sessionmaker, org_id=org_id, project_id=project_a, events=["job.failed"])
        await _hook(
            sessionmaker, org_id=org_id, project_id=project_a, events=["*"], is_active=False
        )
        await _hook(sessionmaker, org_id=uuid4(), project_id=None, events=["*"])  # other org

        async with sessionmaker() as session:
            queued = await emit_event(
                session,
                organization_id=org_id,
                project_id=project_a,
                event="annotation.submitted",
                payload={"item_id": "i1"},
            )
            await session.commit()

        assert queued == 2
        rows = await _deliveries(sessionmaker)
        assert {r.webhook_id for r in rows} == {org_wide.id, only_a.id}
        assert all(r.status is WebhookDeliveryStatus.PENDING for r in rows)
        assert rows[0].payload["event"] == "annotation.submitted"
        assert rows[0].payload["project_id"] == str(project_a)
        assert rows[0].payload["data"] == {"item_id": "i1"}

    async def test_org_level_event_skips_project_hooks(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, project_a, _ = await _seed(sessionmaker)
        await _hook(sessionmaker, org_id=org_id, project_id=project_a, events=["*"])
        org_wide = await _hook(sessionmaker, org_id=org_id, project_id=None, events=["*"])

        async with sessionmaker() as session:
            queued = await emit_event(
                session, organization_id=org_id, project_id=None, event="job.failed", payload={}
            )
            await session.commit()

        assert queued == 1
        assert [r.webhook_id for r in await _deliveries(sessionmaker)] == [org_wide.id]


class _Receiver:
    """A fake subscriber: records requests, answers with a scripted status."""

    def __init__(self, statuses: list[int | Exception]) -> None:
        self.statuses = statuses
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.statuses.pop(0) if self.statuses else 200
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def _queue(
    sessionmaker: async_sessionmaker[AsyncSession], org_id: UUID, project_id: UUID
) -> None:
    async with sessionmaker() as session:
        await emit_event(
            session,
            organization_id=org_id,
            project_id=project_id,
            event="snapshot.created",
            payload={"snapshot_id": "s1"},
        )
        await session.commit()


class TestDeliverDue:
    async def test_success_signs_the_body_and_marks_delivered(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, project_a, _ = await _seed(sessionmaker)
        hook = await _hook(sessionmaker, org_id=org_id, project_id=None, events=["*"])
        await _queue(sessionmaker, org_id, project_a)
        receiver = _Receiver([200])

        async with sessionmaker() as session, receiver.client() as client:
            tally = await deliver_due(session, client, batch_size=10, max_attempts=3)

        assert tally == {"sent": 1, "retried": 0, "failed": 0, "deferred": 0}
        [request] = receiver.requests
        assert str(request.url) == hook.url
        assert request.headers[EVENT_HEADER] == "snapshot.created"
        body = request.content
        assert verify(signing_secret(hook), request.headers[SIGNATURE_HEADER], body) is True
        document = json.loads(body)
        assert document["data"] == {"snapshot_id": "s1"}
        assert document["delivery_id"] == request.headers[DELIVERY_HEADER]

        [row] = await _deliveries(sessionmaker)
        assert row.status is WebhookDeliveryStatus.SUCCEEDED
        assert row.attempts == 1
        assert row.response_status == 200
        assert row.delivered_at is not None
        assert render_body(row) == body
        async with sessionmaker() as session:
            refreshed = await session.get(Webhook, hook.id)
        assert refreshed is not None
        assert refreshed.last_response_status == 200
        assert refreshed.last_delivery_at is not None

    async def test_a_secret_sealed_under_another_key_is_not_sent(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        # `APP_SECRET_KEY` changed: nothing unsigned goes out, the attempt is
        # recorded with a hint to rotate the secret.
        org_id, project_a, _ = await _seed(sessionmaker)
        hook = await _hook(sessionmaker, org_id=org_id, project_id=None, events=["*"])
        async with sessionmaker() as session:
            row = await session.get(Webhook, hook.id)
            assert row is not None
            row.secret = "not-a-sealed-value"
            await session.commit()
        await _queue(sessionmaker, org_id, project_a)
        receiver = _Receiver([200])

        async with sessionmaker() as session, receiver.client() as client:
            tally = await deliver_due(session, client, batch_size=10, max_attempts=3)

        assert tally == {"sent": 0, "retried": 1, "failed": 0, "deferred": 0}
        assert receiver.requests == []
        [delivery] = await _deliveries(sessionmaker)
        assert delivery.error is not None
        assert "rotate the webhook secret" in delivery.error

    async def test_a_hook_over_its_minute_budget_waits_without_losing_attempts(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, project_a, _ = await _seed(sessionmaker)
        await _hook(sessionmaker, org_id=org_id, project_id=None, events=["*"])
        for _ in range(5):
            await _queue(sessionmaker, org_id, project_a)
        receiver = _Receiver([])
        now = datetime.now(UTC)

        async with sessionmaker() as session, receiver.client() as client:
            tally = await deliver_due(
                session, client, batch_size=10, max_attempts=3, now=now, per_hook_per_minute=2
            )

        assert tally == {"sent": 2, "retried": 0, "failed": 0, "deferred": 3}
        assert len(receiver.requests) == 2
        waiting = [d for d in await _deliveries(sessionmaker) if d.status.value == "pending"]
        assert len(waiting) == 3
        for row in waiting:
            assert row.attempts == 0
            stamp = row.next_attempt_at
            stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
            assert stamp > now

    async def test_a_backlog_on_one_hook_does_not_crowd_out_another(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, project_a, _ = await _seed(sessionmaker)
        await _hook(sessionmaker, org_id=org_id, project_id=None, events=["*"])
        for _ in range(6):
            await _queue(sessionmaker, org_id, project_a)
        await _hook(
            sessionmaker,
            org_id=org_id,
            project_id=None,
            events=["*"],
            url="https://other.example/in",
        )
        await _queue(sessionmaker, org_id, project_a)  # one each for both hooks
        receiver = _Receiver([])

        async with sessionmaker() as session, receiver.client() as client:
            await deliver_due(session, client, batch_size=3, max_attempts=3, per_hook_per_batch=2)

        hosts = [request.url.host for request in receiver.requests]
        assert hosts.count("hooks.example") == 2
        assert hosts.count("other.example") == 1

    async def test_failures_back_off_then_give_up(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, project_a, _ = await _seed(sessionmaker)
        await _hook(sessionmaker, org_id=org_id, project_id=None, events=["*"])
        await _queue(sessionmaker, org_id, project_a)
        receiver = _Receiver([500, httpx.ConnectError("refused"), 404])
        now = datetime(2030, 1, 1, tzinfo=UTC)

        async def tick(at: datetime) -> dict[str, int]:
            async with sessionmaker() as session, receiver.client() as client:
                return await deliver_due(session, client, batch_size=10, max_attempts=3, now=at)

        assert await tick(now) == {"sent": 0, "retried": 1, "failed": 0, "deferred": 0}
        [row] = await _deliveries(sessionmaker)
        assert row.error == "HTTP 500"
        assert row.next_attempt_at.replace(tzinfo=UTC) == now + timedelta(seconds=30)

        # Not due yet: nothing happens.
        assert await tick(now + timedelta(seconds=10)) == {
            "sent": 0,
            "retried": 0,
            "failed": 0,
            "deferred": 0,
        }

        assert await tick(now + timedelta(seconds=30)) == {
            "sent": 0,
            "retried": 1,
            "failed": 0,
            "deferred": 0,
        }
        [row] = await _deliveries(sessionmaker)
        assert row.attempts == 2
        assert "ConnectError" in (row.error or "")
        assert row.response_status is None
        assert row.next_attempt_at.replace(tzinfo=UTC) == now + timedelta(seconds=90)

        assert await tick(now + timedelta(seconds=90)) == {
            "sent": 0,
            "retried": 0,
            "failed": 1,
            "deferred": 0,
        }
        [row] = await _deliveries(sessionmaker)
        assert row.status is WebhookDeliveryStatus.FAILED
        assert row.attempts == 3
        assert row.error == "HTTP 404"
        assert len(receiver.requests) == 3

    async def test_inactive_hook_drops_its_pending_deliveries(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, project_a, _ = await _seed(sessionmaker)
        hook = await _hook(sessionmaker, org_id=org_id, project_id=None, events=["*"])
        await _queue(sessionmaker, org_id, project_a)
        async with sessionmaker() as session:
            row = await session.get(Webhook, hook.id)
            assert row is not None
            row.is_active = False
            await session.commit()
        receiver = _Receiver([])

        async with sessionmaker() as session, receiver.client() as client:
            tally = await deliver_due(session, client, batch_size=10, max_attempts=3)

        assert tally == {"sent": 0, "retried": 0, "failed": 1, "deferred": 0}
        assert receiver.requests == []
        [delivery] = await _deliveries(sessionmaker)
        assert delivery.error == "webhook inactive"

    async def test_batch_size_limits_one_tick(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, project_a, _ = await _seed(sessionmaker)
        await _hook(sessionmaker, org_id=org_id, project_id=None, events=["*"])
        for _ in range(3):
            await _queue(sessionmaker, org_id, project_a)
        receiver = _Receiver([])

        async with sessionmaker() as session, receiver.client() as client:
            tally = await deliver_due(session, client, batch_size=2, max_attempts=3)

        assert tally["sent"] == 2
        statuses: list[Any] = [d.status for d in await _deliveries(sessionmaker)]
        assert statuses.count(WebhookDeliveryStatus.PENDING) == 1


class TestChatFormats:
    """API-7: a `slack` / `teams` hook gets a chat message naming the project."""

    @pytest.mark.parametrize("hook_format", [WebhookFormat.SLACK, WebhookFormat.TEAMS])
    async def test_chat_hooks_get_a_message_with_the_project_name(
        self, sessionmaker: async_sessionmaker[AsyncSession], hook_format: WebhookFormat
    ) -> None:
        org_id, project_a, _ = await _seed(sessionmaker)
        hook = await _hook(
            sessionmaker, org_id=org_id, project_id=None, events=["*"], hook_format=hook_format
        )
        await _queue(sessionmaker, org_id, project_a)
        receiver = _Receiver([200])

        async with sessionmaker() as session, receiver.client() as client:
            tally = await deliver_due(
                session,
                client,
                batch_size=10,
                max_attempts=3,
                frontend_url="https://annotate.example/",
            )

        assert tally["sent"] == 1
        [request] = receiver.requests
        # Signed like every format, so a relay can still verify it.
        assert verify(signing_secret(hook), request.headers[SIGNATURE_HEADER], request.content)
        body = json.loads(request.content)
        text = json.dumps(body)
        assert "Snapshot" in text
        assert f"https://annotate.example/projects/{project_a}" in text
        if hook_format is WebhookFormat.SLACK:
            assert body["text"].startswith("a: Snapshot")
            assert body["blocks"][0]["text"]["text"].startswith("*a*")
        else:
            card = body["attachments"][0]["content"]
            assert body["type"] == "message"
            assert card["type"] == "AdaptiveCard"
            assert card["body"][0]["text"] == "a"


class TestDestinationGuard:
    """SEC-4: a hook cannot be pointed at internal addresses."""

    @pytest.mark.parametrize(
        ("url", "reason"),
        [
            ("http://127.0.0.1:8000/x", "loopback"),
            ("http://[::1]/x", "loopback"),
            ("http://169.254.169.254/latest/meta-data", "link-local"),
            ("http://10.0.0.7/x", "private"),
            ("http://192.168.1.2/x", "private"),
            ("http://[::ffff:10.0.0.1]/x", "private"),
            ("http://0.0.0.0/x", "unspecified"),
            ("ftp://example.com/x", "not an http(s) URL"),
        ],
    )
    async def test_internal_destinations_are_refused(self, url: str, reason: str) -> None:
        refused = await public_destination(url)
        assert refused is not None
        assert reason in refused

    async def test_a_public_address_is_allowed(self) -> None:
        assert await public_destination("https://93.184.216.34/hook") is None

    async def test_a_refused_delivery_counts_an_attempt_and_sends_nothing(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id, project_a, _ = await _seed(sessionmaker)
        await _hook(
            sessionmaker,
            org_id=org_id,
            project_id=None,
            events=["*"],
            url="http://169.254.169.254/latest",
        )
        await _queue(sessionmaker, org_id, project_a)
        receiver = _Receiver([200])

        async with sessionmaker() as session, receiver.client() as client:
            tally = await deliver_due(
                session, client, batch_size=10, max_attempts=3, guard=public_destination
            )

        assert tally == {"sent": 0, "retried": 1, "failed": 0, "deferred": 0}
        assert receiver.requests == []
        [row] = await _deliveries(sessionmaker)
        assert row.attempts == 1
        assert row.error == "destination not allowed: link-local address 169.254.169.254"


def _resolve_to(monkeypatch: pytest.MonkeyPatch, *answers: list[str]) -> list[str]:
    """Make the running loop's getaddrinfo answer each call with the next list
    of addresses (the last one repeats). Returns the names asked for."""
    asked: list[str] = []
    loop = asyncio.get_running_loop()

    async def getaddrinfo(host: str, port: int, **_: Any) -> list[Any]:
        asked.append(host)
        addresses = answers[min(len(asked), len(answers)) - 1]
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)) for address in addresses
        ]

    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    return asked


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    """Inner backend that records what it was asked to dial and refuses it."""

    def __init__(self) -> None:
        self.dialled: list[str] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        self.dialled.append(host)
        raise httpcore.ConnectError("refused")

    async def sleep(self, seconds: float) -> None:
        return None


class TestConnectTimeGuard:
    """SEC-4: the address actually dialled is checked, not only the pre-send lookup."""

    def test_public_only_transport_uses_the_guarded_backend(self) -> None:
        # Pins the httpx / httpcore internals public_only_transport patches.
        transport = public_only_transport()
        assert isinstance(transport._pool._network_backend, _PublicOnlyBackend)

    async def test_dns_rebinding_is_refused_at_connect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Public for the pre-send check, loopback when the client connects.
        asked = _resolve_to(monkeypatch, ["93.184.216.34"], ["127.0.0.1"])
        url = "http://rebind.example/hook"
        assert await public_destination(url) is None

        async with httpx.AsyncClient(transport=public_only_transport(), trust_env=False) as client:
            with pytest.raises(httpx.ConnectError, match=re.escape("loopback address 127.0.0.1")):
                await client.post(url, content=b"{}")
        assert asked == ["rebind.example", "rebind.example"]

    async def test_one_internal_record_refuses_the_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _resolve_to(monkeypatch, ["93.184.216.34", "10.0.0.5"])
        inner = _RecordingBackend()
        backend = _PublicOnlyBackend(inner)

        with pytest.raises(httpcore.ConnectError, match=re.escape("private address 10.0.0.5")):
            await backend.connect_tcp("mixed.example", 443)
        assert inner.dialled == []

    async def test_the_checked_address_is_dialled_not_the_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _resolve_to(monkeypatch, ["93.184.216.34", "93.184.216.35", "93.184.216.34"])
        inner = _RecordingBackend()
        backend = _PublicOnlyBackend(inner)

        # Each address is tried once, in order, and the last error surfaces.
        with pytest.raises(httpcore.ConnectError, match="refused"):
            await backend.connect_tcp("hooks.example", 443)
        assert inner.dialled == ["93.184.216.34", "93.184.216.35"]

    async def test_an_unresolvable_name_is_a_connect_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fail(*_: Any, **__: Any) -> list[Any]:
            raise socket.gaierror("no such host")

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", fail)
        with pytest.raises(httpcore.ConnectError, match="cannot resolve nowhere"):
            await _PublicOnlyBackend(_RecordingBackend()).connect_tcp("nowhere.example", 443)

    async def test_unix_sockets_are_refused(self) -> None:
        with pytest.raises(httpcore.ConnectError, match="unix socket"):
            await _PublicOnlyBackend().connect_unix_socket("/var/run/docker.sock")

    @pytest.mark.parametrize("allow_private", [False, True])
    async def test_worker_tick_builds_the_client_either_way(
        self, sessionmaker: async_sessionmaker[AsyncSession], allow_private: bool
    ) -> None:
        from app.worker.webhooks import deliver_webhooks

        settings = SimpleNamespace(
            webhook_allow_private_urls=allow_private,
            webhook_timeout=5,
            webhook_poll_batch_size=10,
            webhook_max_per_minute=60,
            webhook_max_attempts=3,
            frontend_url="http://localhost:5173",
        )
        tally = await deliver_webhooks({"sessionmaker": sessionmaker, "settings": settings})
        assert tally == {"sent": 0, "retried": 0, "failed": 0, "deferred": 0}
