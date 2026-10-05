"""Tests for the `/connectors` router.

No live database: each test gets a fresh in-memory SQLite database (an async
engine on a `StaticPool`, so the same connection survives across the
requests a single test makes) with only the `connector` table, and
`app.api.deps.get_current_user` is overridden with a fake `CurrentUser`
instead of decoding a real JWT.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Coroutine, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser, get_current_user
from app.connectors.base import ConnectorCheck
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import AuditEvent, Connector, ConnectorIdentity, ConnectorType

ORG_ID = uuid.uuid4()
OTHER_ORG_ID = uuid.uuid4()
ADMIN_ID = uuid.uuid4()

_TABLES = cast("list[Table]", [AuditEvent.__table__, Connector.__table__])


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


@pytest.fixture
def engine() -> Iterator[Any]:
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    async def _create() -> None:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=_TABLES)

    _run(_create())
    yield eng
    _run(eng.dispose())


@pytest.fixture
def sessionmaker(engine: Any) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def app(sessionmaker: async_sessionmaker[AsyncSession]) -> FastAPI:
    application = create_app()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_session] = _get_session
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _login(
    app: FastAPI,
    *,
    user_id: uuid.UUID = ADMIN_ID,
    organization_id: uuid.UUID = ORG_ID,
    is_superuser: bool = True,
) -> None:
    user = CurrentUser(
        id=user_id,
        organization_id=organization_id,
        email="admin@example.com",
        is_superuser=is_superuser,
        is_service=False,
        scopes=frozenset(),
    )
    app.dependency_overrides[get_current_user] = lambda: user


def _seed_connector(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    organization_id: uuid.UUID = ORG_ID,
    name: str = "conn",
    type_: ConnectorType = ConnectorType.LOCAL,
    identity_type: ConnectorIdentity = ConnectorIdentity.NONE,
    secret_ref: str | None = None,
    config: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> Connector:
    moment = created_at or datetime.now(UTC)

    async def _create() -> Connector:
        async with sessionmaker() as session:
            connector = Connector(
                organization_id=organization_id,
                name=name,
                type=type_,
                identity_type=identity_type,
                secret_ref=secret_ref,
                config=config or {},
                created_at=moment,
                updated_at=moment,
            )
            session.add(connector)
            await session.commit()
            await session.refresh(connector)
            return connector

    return _run(_create())


class TestList:
    def test_list_scoped_to_organization(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _seed_connector(sessionmaker, organization_id=ORG_ID, name="mine")
        _seed_connector(sessionmaker, organization_id=OTHER_ORG_ID, name="not-mine")
        _login(app)

        response = client.get("/api/v1/connectors")
        assert response.status_code == 200
        names = [item["name"] for item in response.json()["items"]]
        assert names == ["mine"]

    def test_cursor_pagination_round_trip(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for i in range(3):
            _seed_connector(sessionmaker, name=f"c{i}", created_at=base + timedelta(seconds=i))
        _login(app)

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(3):
            params = {"limit": 1, **({"cursor": cursor} if cursor else {})}
            response = client.get("/api/v1/connectors", params=params)
            assert response.status_code == 200
            body = response.json()
            assert len(body["items"]) == 1
            seen.append(body["items"][0]["name"])
            cursor = body["next_cursor"]

        assert seen == ["c2", "c1", "c0"]
        assert cursor is None

    def test_401_without_token(self, client: TestClient) -> None:
        response = client.get("/api/v1/connectors")
        assert response.status_code == 401

    def test_403_for_non_superuser(self, app: FastAPI, client: TestClient) -> None:
        _login(app, is_superuser=False)
        response = client.get("/api/v1/connectors")
        assert response.status_code == 403


class TestCreateAndRead:
    def test_create_and_get_never_expose_secret_ref(self, app: FastAPI, client: TestClient) -> None:
        _login(app)
        response = client.post(
            "/api/v1/connectors",
            json={
                "name": "s3-prod",
                "type": "s3",
                "identity_type": "iam_role",
                "secret_ref": "AWS_PROD_SECRET",
                "config": {"bucket": "prod-data"},
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["has_secret"] is True
        assert "secret_ref" not in body

        fetched = client.get(f"/api/v1/connectors/{body['id']}")
        assert fetched.status_code == 200
        assert "secret_ref" not in fetched.json()
        assert fetched.json()["has_secret"] is True

    def test_has_secret_false_without_one(self, app: FastAPI, client: TestClient) -> None:
        _login(app)
        response = client.post(
            "/api/v1/connectors",
            json={"name": "local-dev", "type": "local", "identity_type": "none", "config": {}},
        )
        assert response.status_code == 201
        assert response.json()["has_secret"] is False

    def test_404_for_another_organizations_connector(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        connector = _seed_connector(sessionmaker, organization_id=OTHER_ORG_ID)
        _login(app, organization_id=ORG_ID)

        response = client.get(f"/api/v1/connectors/{connector.id}")
        assert response.status_code == 404


class TestUpdateAndDelete:
    def test_patch_updates_only_given_fields(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        connector = _seed_connector(sessionmaker, name="old-name")
        _login(app)

        response = client.patch(f"/api/v1/connectors/{connector.id}", json={"name": "new-name"})
        assert response.status_code == 200
        assert response.json()["name"] == "new-name"

    def test_delete(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        connector = _seed_connector(sessionmaker)
        _login(app)

        response = client.delete(f"/api/v1/connectors/{connector.id}")
        assert response.status_code == 204

        follow_up = client.get(f"/api/v1/connectors/{connector.id}")
        assert follow_up.status_code == 404

    def test_403_for_non_superuser(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        connector = _seed_connector(sessionmaker)
        _login(app, is_superuser=False)

        response = client.patch(f"/api/v1/connectors/{connector.id}", json={"name": "nope"})
        assert response.status_code == 403


class TestCheck:
    def test_ok_for_a_reachable_local_connector(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        connector = _seed_connector(
            sessionmaker, type_=ConnectorType.LOCAL, config={"root": str(tmp_path)}
        )
        _login(app)

        response = client.post(f"/api/v1/connectors/{connector.id}/check")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True
        assert body["messages"]

    def test_not_ok_when_secret_ref_cannot_be_resolved(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        connector = _seed_connector(
            sessionmaker,
            type_=ConnectorType.LOCAL,
            secret_ref="DOES_NOT_EXIST_IN_ENV_XYZ",
            config={"root": "/tmp"},
        )
        _login(app)

        response = client.post(f"/api/v1/connectors/{connector.id}/check")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert "DOES_NOT_EXIST_IN_ENV_XYZ" in body["messages"][0]

    def test_never_500s_on_a_misconfigured_connector(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        # azure_blob requires account_url/container/identity_type in config;
        # this row has none of them, so `build_connector` raises.
        connector = _seed_connector(
            sessionmaker, type_=ConnectorType.AZURE_BLOB, config={}, name="broken"
        )
        _login(app)

        response = client.post(f"/api/v1/connectors/{connector.id}/check")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert body["messages"]

    def test_never_500s_on_a_timeout(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        connector = _seed_connector(
            sessionmaker, type_=ConnectorType.LOCAL, config={"root": "/tmp"}
        )
        _login(app)

        class _SlowConnector:
            async def check(self) -> ConnectorCheck:
                await asyncio.sleep(1)
                return ConnectorCheck(ok=True, messages=["should never get here"])

        monkeypatch.setattr(
            "app.api.v1.connectors.build_connector", lambda *args, **kwargs: _SlowConnector()
        )
        monkeypatch.setattr("app.api.v1.connectors._CHECK_TIMEOUT_SECONDS", 0.01)

        response = client.post(f"/api/v1/connectors/{connector.id}/check")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
