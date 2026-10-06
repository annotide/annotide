"""Tests for the storage-event receiver and its token (SRC-3).

`/connectors/{id}/events/token` is superuser work; `/connectors/{id}/events`
takes no session at all, only the token, and queues `scan_source` jobs with
a `paths` payload on the `FakeJobQueue`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Coroutine, Iterator
from datetime import date
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser, get_current_user, get_effective_license
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import (
    AuditEvent,
    Connector,
    ConnectorIdentity,
    ConnectorType,
    Job,
    JobStatus,
    JobType,
    Project,
)
from app.services import discovery
from app.services.licensing.issue import generate_keypair, issue
from app.services.licensing.state import EffectiveLicense, KeySource, resolve
from app.services.queue import get_job_queue
from tests.support import FakeJobQueue

ORG_ID = uuid.uuid4()
OTHER_ORG_ID = uuid.uuid4()
ADMIN_ID = uuid.uuid4()

_TABLES = cast(
    "list[Table]",
    [AuditEvent.__table__, Connector.__table__, Project.__table__, Job.__table__],
)


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


def _licence(today: date) -> EffectiveLicense:
    private_key, public_key = generate_keypair()
    key = issue(
        private_key,
        {
            "v": 1,
            "kid": "vevents",
            "lic": "66666666-6666-6666-6666-666666666666",
            "tier": "commercial",
            "licensee": "Acme Oy",
            "seats": 5,
            "issued_at": "2025-07-01",
            "expires_at": "2026-06-30",
            "features": [],
        },
    )
    return resolve([(KeySource.ENV, key)], today=today, public_keys={"vevents": public_key})


@pytest.fixture
def sessionmaker() -> Iterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    async def _create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=_TABLES)

    _run(_create())
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    _run(engine.dispose())


@pytest.fixture
def queue() -> FakeJobQueue:
    return FakeJobQueue()


@pytest.fixture
def app(sessionmaker: async_sessionmaker[AsyncSession], queue: FakeJobQueue) -> FastAPI:
    application = create_app()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    async def _get_queue() -> FakeJobQueue:
        return queue

    in_force = _licence(date(2026, 6, 1))
    application.dependency_overrides[get_session] = _get_session
    application.dependency_overrides[get_job_queue] = _get_queue
    application.dependency_overrides[get_effective_license] = lambda: in_force
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _login(app: FastAPI, *, is_superuser: bool = True, organization_id: uuid.UUID = ORG_ID) -> None:
    user = CurrentUser(
        id=ADMIN_ID,
        organization_id=organization_id,
        email="admin@example.com",
        is_superuser=is_superuser,
        is_service=False,
        scopes=frozenset(),
    )
    app.dependency_overrides[get_current_user] = lambda: user


def _seed(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    token: str | None = None,
    projects: tuple[str, ...] = ("",),
) -> tuple[Connector, list[Project]]:
    async def _create() -> tuple[Connector, list[Project]]:
        async with sessionmaker() as session:
            connector = Connector(
                organization_id=ORG_ID,
                name="shots",
                type=ConnectorType.S3,
                identity_type=ConnectorIdentity.NONE,
                config={"bucket": "shots"},
                event_token_hash=discovery.hash_event_token(token) if token else None,
            )
            session.add(connector)
            await session.flush()
            rows = [
                Project(
                    organization_id=ORG_ID,
                    name=f"p{n}",
                    source_connector_id=connector.id,
                    source_prefix=prefix,
                    settings={},
                    workflow={},
                )
                for n, prefix in enumerate(projects)
            ]
            session.add_all(rows)
            await session.commit()
            return connector, rows

    return _run(_create())


def _jobs(sessionmaker: async_sessionmaker[AsyncSession]) -> list[Job]:
    async def _load() -> list[Job]:
        async with sessionmaker() as session:
            return list(await session.scalars(select(Job)))

    return _run(_load())


def _s3(*keys: str, name: str = "ObjectCreated:Put") -> dict[str, Any]:
    return {
        "Records": [
            {"eventName": name, "s3": {"bucket": {"name": "shots"}, "object": {"key": key}}}
            for key in keys
        ]
    }


TOKEN = "evt_test-token"


# --------------------------------------------------------------------------- #
# Token management
# --------------------------------------------------------------------------- #


class TestToken:
    def test_minting_turns_events_on_and_shows_the_token_once(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        connector, _ = _seed(sessionmaker)
        _login(app)

        response = client.post(f"/api/v1/connectors/{connector.id}/events/token")
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["path"] == f"/api/v1/connectors/{connector.id}/events"
        assert body["token"].startswith("evt_")

        listed = client.get(f"/api/v1/connectors/{connector.id}").json()
        assert listed["events_enabled"] is True
        assert body["token"] not in json.dumps(listed)

        # Replacing the token retires the old one at once.
        again = client.post(f"/api/v1/connectors/{connector.id}/events/token").json()
        assert again["token"] != body["token"]
        old = client.post(f"{body['path']}?token={body['token']}", json=_s3("a.png"))
        assert old.status_code == 404
        new = client.post(f"{again['path']}?token={again['token']}", json=_s3("a.png"))
        assert new.status_code == 202, new.text

        async def _audit() -> list[dict[str, Any] | None]:
            async with sessionmaker() as session:
                rows = await session.scalars(
                    select(AuditEvent).where(AuditEvent.action == "connector.events_token")
                )
                return [row.after for row in rows]

        after = _run(_audit())
        assert after == [
            {"events_enabled": True, "replaced": False},
            {"events_enabled": True, "replaced": True},
        ]
        assert body["token"] not in json.dumps(after)

    def test_revoking_turns_events_off(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        connector, _ = _seed(sessionmaker, token=TOKEN)
        _login(app)

        assert client.delete(f"/api/v1/connectors/{connector.id}/events/token").status_code == 204
        assert client.get(f"/api/v1/connectors/{connector.id}").json()["events_enabled"] is False
        response = client.post(
            f"/api/v1/connectors/{connector.id}/events?token={TOKEN}", json=_s3("a.png")
        )
        assert response.status_code == 404
        # Idempotent.
        assert client.delete(f"/api/v1/connectors/{connector.id}/events/token").status_code == 204

    def test_only_a_superuser_of_the_organisation_manages_it(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        connector, _ = _seed(sessionmaker)
        path = f"/api/v1/connectors/{connector.id}/events/token"

        _login(app, is_superuser=False)
        assert client.post(path).status_code == 403
        _login(app, organization_id=OTHER_ORG_ID)
        assert client.post(path).status_code == 404
        assert client.delete(path).status_code == 404


# --------------------------------------------------------------------------- #
# Receiver
# --------------------------------------------------------------------------- #


class TestReceiver:
    def test_created_objects_queue_one_scan_per_project(
        self,
        client: TestClient,
        queue: FakeJobQueue,
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        connector, (north, south, _elsewhere) = _seed(
            sessionmaker, token=TOKEN, projects=("north/", "south/", "west/")
        )

        response = client.post(
            f"/api/v1/connectors/{connector.id}/events?token={TOKEN}",
            json=_s3("north/a.png", "north/b.png", "south/c.jpg", "east/d.png", "north/notes.xyz"),
        )

        assert response.status_code == 202, response.text
        body = response.json()
        assert (body["received"], body["matched"], body["ignored"]) == (5, 3, 2)
        jobs = {job.project_id: job for job in _jobs(sessionmaker)}
        assert set(jobs) == {north.id, south.id}
        assert jobs[north.id].type is JobType.SCAN_SOURCE
        assert jobs[north.id].status is JobStatus.QUEUED
        assert jobs[north.id].payload == {
            "paths": ["north/a.png", "north/b.png"],
            "trigger": "event",
        }
        assert jobs[south.id].payload == {"paths": ["south/c.jpg"], "trigger": "event"}
        assert sorted(body["job_ids"]) == sorted(str(job.id) for job in jobs.values())
        assert {job_id for job_id, _ in queue.enqueued} == {job.id for job in jobs.values()}

    def test_the_token_may_come_in_a_header(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        connector, _ = _seed(sessionmaker, token=TOKEN)
        response = client.post(
            f"/api/v1/connectors/{connector.id}/events",
            json=_s3("a.png"),
            headers={"X-Event-Token": TOKEN},
        )
        assert response.status_code == 202, response.text
        assert len(response.json()["job_ids"]) == 1

    def test_nothing_to_pick_up_queues_nothing(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        connector, _ = _seed(sessionmaker, token=TOKEN)
        response = client.post(
            f"/api/v1/connectors/{connector.id}/events?token={TOKEN}",
            json=_s3("a.png", name="ObjectRemoved:Delete"),
        )
        assert response.status_code == 202
        assert response.json() == {"received": 1, "matched": 0, "ignored": 1, "job_ids": []}
        assert _jobs(sessionmaker) == []

    @pytest.mark.parametrize(
        "query", ["", "?token=wrong", f"?token={TOKEN}&unknown=1"], ids=["none", "wrong", "other"]
    )
    def test_a_missing_or_wrong_token_and_an_unknown_connector_look_alike(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession], query: str
    ) -> None:
        connector, _ = _seed(sessionmaker, token=TOKEN)
        target = connector.id if query != f"?token={TOKEN}&unknown=1" else uuid.uuid4()
        response = client.post(f"/api/v1/connectors/{target}/events{query}", json=_s3("a.png"))
        assert response.status_code == 404
        assert _jobs(sessionmaker) == []

    def test_events_off_answer_404_even_to_an_empty_token(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        connector, _ = _seed(sessionmaker)
        response = client.post(f"/api/v1/connectors/{connector.id}/events?token=", json=_s3("a"))
        assert response.status_code == 404

    def test_sns_posts_text_plain(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        connector, _ = _seed(sessionmaker, token=TOKEN)
        body = {"Type": "Notification", "Message": json.dumps(_s3("a.png"))}
        response = client.post(
            f"/api/v1/connectors/{connector.id}/events?token={TOKEN}",
            content=json.dumps(body),
            headers={"Content-Type": "text/plain; charset=UTF-8"},
        )
        assert response.status_code == 202, response.text
        assert response.json()["matched"] == 1

    @pytest.mark.parametrize("content", [b"not json", b'{"hello": "world"}'])
    def test_an_unrecognised_body_is_422(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession], content: bytes
    ) -> None:
        connector, _ = _seed(sessionmaker, token=TOKEN)
        response = client.post(
            f"/api/v1/connectors/{connector.id}/events?token={TOKEN}", content=content
        )
        assert response.status_code == 422
        assert "Unrecognised storage event" in response.json()["detail"]

    def test_limits(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        connector, _ = _seed(sessionmaker, token=TOKEN)
        url = f"/api/v1/connectors/{connector.id}/events?token={TOKEN}"

        monkeypatch.setattr(discovery, "MAX_EVENT_OBJECTS", 2)
        assert client.post(url, json=_s3("a.png", "b.png", "c.png")).status_code == 413
        monkeypatch.setattr(discovery, "MAX_EVENT_BODY_BYTES", 10)
        assert client.post(url, json=_s3("a.png")).status_code == 413
        # Streamed without a Content-Length: counted as it arrives.
        chunks = iter([b'{"Records": ', b"[]" * 10, b"}"])
        assert client.post(url, content=chunks).status_code == 413
        assert _jobs(sessionmaker) == []

    def test_json_nested_too_deep_is_422_not_500(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        connector, _ = _seed(sessionmaker, token=TOKEN)
        response = client.post(
            f"/api/v1/connectors/{connector.id}/events?token={TOKEN}", content=b"[" * 100_000
        )
        assert response.status_code == 422

    def test_restricted_licence_answers_503_so_the_sender_retries(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        connector, _ = _seed(sessionmaker, token=TOKEN)
        restricted = _licence(date(2026, 7, 31))
        app.dependency_overrides[get_effective_license] = lambda: restricted

        response = client.post(
            f"/api/v1/connectors/{connector.id}/events?token={TOKEN}", json=_s3("a.png")
        )
        assert response.status_code == 503
        assert _jobs(sessionmaker) == []


# --------------------------------------------------------------------------- #
# Handshakes
# --------------------------------------------------------------------------- #


class TestHandshakes:
    def test_event_grid_validation_echoes_the_code(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        connector, _ = _seed(sessionmaker, token=TOKEN)
        response = client.post(
            f"/api/v1/connectors/{connector.id}/events?token={TOKEN}",
            json=[
                {
                    "eventType": "Microsoft.EventGrid.SubscriptionValidationEvent",
                    "data": {"validationCode": "abc-123"},
                }
            ],
        )
        assert response.status_code == 200
        assert response.json() == {"validationResponse": "abc-123"}

    def test_cloud_events_options_handshake(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        connector, _ = _seed(sessionmaker, token=TOKEN)
        path = f"/api/v1/connectors/{connector.id}/events"
        origin = {"WebHook-Request-Origin": "eventgrid.azure.net"}

        response = client.options(f"{path}?token={TOKEN}", headers=origin)
        assert response.status_code == 200
        assert response.headers["WebHook-Allowed-Origin"] == "eventgrid.azure.net"
        assert client.options(f"{path}?token=wrong", headers=origin).status_code == 404

    def test_sns_subscription_is_confirmed(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        connector, _ = _seed(sessionmaker, token=TOKEN)
        confirmed: list[str] = []

        async def _confirm(url: str) -> None:
            confirmed.append(url)

        monkeypatch.setattr(discovery, "confirm_sns_subscription", _confirm)
        url = "https://sns.eu-west-1.amazonaws.com/?Action=ConfirmSubscription&Token=t"
        response = client.post(
            f"/api/v1/connectors/{connector.id}/events?token={TOKEN}",
            content=json.dumps({"Type": "SubscriptionConfirmation", "SubscribeURL": url}),
        )
        assert response.status_code == 200
        assert response.json() == {"confirmed": True}
        assert confirmed == [url]

    def test_a_foreign_subscribe_url_is_refused(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        connector, _ = _seed(sessionmaker, token=TOKEN)
        response = client.post(
            f"/api/v1/connectors/{connector.id}/events?token={TOKEN}",
            json={"Type": "SubscriptionConfirmation", "SubscribeURL": "https://example.com/x"},
        )
        assert response.status_code == 422

    def test_an_unreachable_sns_is_503(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        connector, _ = _seed(sessionmaker, token=TOKEN)

        async def _down(url: str) -> None:
            raise httpx.ConnectError("no route")

        monkeypatch.setattr(discovery, "confirm_sns_subscription", _down)
        response = client.post(
            f"/api/v1/connectors/{connector.id}/events?token={TOKEN}",
            json={
                "Type": "SubscriptionConfirmation",
                "SubscribeURL": "https://sns.eu-west-1.amazonaws.com/?Action=ConfirmSubscription",
            },
        )
        assert response.status_code == 503
