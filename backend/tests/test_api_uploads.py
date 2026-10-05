"""Tests for `POST /projects/{id}/uploads` and `services/uploads.py` (§12 upload path).

The endpoint mints write-scoped signed URLs on the project's *source*
connector; the browser does the PUTs. With a `local` connector the URLs
point back at the API's proxy, so the full round trip — mint, PUT, file
on disk — runs here without any cloud SDK.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Coroutine, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser, get_current_user
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import (
    AuditEvent,
    Connector,
    ConnectorIdentity,
    ConnectorType,
    Membership,
    Project,
    ProjectRole,
)
from app.services.uploads import InvalidUploadPath, join_prefix, normalize_upload_path

ORG_ID = uuid.uuid4()
MEMBER_ID = uuid.uuid4()

_TABLES = cast(
    "list[Table]",
    [AuditEvent.__table__, Project.__table__, Connector.__table__, Membership.__table__],
)

_PIXEL = b"\x89PNG\r\n\x1a\n" + b"fake-but-recognisable"


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


def _login(app: FastAPI) -> None:
    user = CurrentUser(
        id=MEMBER_ID,
        organization_id=ORG_ID,
        email="member@example.com",
        is_superuser=False,
        is_service=False,
        scopes=frozenset(),
    )
    app.dependency_overrides[get_current_user] = lambda: user


def _seed(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    root: Path | None,
    connector_type: ConnectorType = ConnectorType.LOCAL,
    source_prefix: str | None = None,
    role: ProjectRole = ProjectRole.OWNER,
) -> Project:
    async def _create() -> Project:
        async with sessionmaker() as session:
            connector_id: uuid.UUID | None = None
            if root is not None:
                connector = Connector(
                    organization_id=ORG_ID,
                    name="source",
                    type=connector_type,
                    identity_type=ConnectorIdentity.NONE,
                    config={"root": str(root)},
                )
                session.add(connector)
                await session.flush()
                connector_id = connector.id
            project = Project(
                organization_id=ORG_ID,
                name="proj",
                source_connector_id=connector_id,
                source_prefix=source_prefix,
                settings={},
                workflow={},
            )
            session.add(project)
            await session.flush()
            session.add(Membership(user_id=MEMBER_ID, project_id=project.id, role=role))
            await session.commit()
            await session.refresh(project)
            return project

    return _run(_create())


class TestNormalizeUploadPath:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("a.jpg", "a.jpg"),
            ("photos/2024/a.jpg", "photos/2024/a.jpg"),
            ("./photos/a.jpg", "photos/a.jpg"),
            ("photos\\sub\\a.jpg", "photos/sub/a.jpg"),
            ("  a.jpg ", "a.jpg"),
            ("photos//a.jpg", "photos/a.jpg"),
        ],
    )
    def test_accepts_relative_paths(self, raw: str, expected: str) -> None:
        assert normalize_upload_path(raw) == expected

    @pytest.mark.parametrize(
        "raw", ["", "   ", "/etc/passwd", "../a.jpg", "photos/../../a.jpg", "C:/x.jpg", "a\x00.jpg"]
    )
    def test_rejects_escapes(self, raw: str) -> None:
        with pytest.raises(InvalidUploadPath):
            normalize_upload_path(raw)

    def test_join_prefix(self) -> None:
        assert join_prefix(None, "a.jpg") == "a.jpg"
        assert join_prefix("", "a.jpg") == "a.jpg"
        assert join_prefix("/in/", "a.jpg") == "in/a.jpg"
        assert join_prefix("in", "sub/a.jpg") == "in/sub/a.jpg"


class TestCreateUploadUrls:
    def test_mints_one_target_per_file_under_the_source_prefix(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        project = _seed(sessionmaker, root=tmp_path, source_prefix="incoming/")
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/uploads",
            json={
                "files": [
                    {"path": "a.jpg", "content_type": "image/jpeg", "size_bytes": 10},
                    {"path": "batch1/b.png"},
                ]
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["prefix"] == "incoming/"
        paths = [target["path"] for target in body["uploads"]]
        assert paths == ["incoming/a.jpg", "incoming/batch1/b.png"]
        first, second = body["uploads"]
        assert first["method"] == "PUT"
        assert first["headers"]["Content-Type"] == "image/jpeg"
        assert second["headers"]["Content-Type"] == "application/octet-stream"
        assert (
            f"/api/v1/storage/local/{project.source_connector_id}/incoming/a.jpg?" in first["url"]
        )

    def test_round_trip_puts_the_file_where_a_scan_will_find_it(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        project = _seed(sessionmaker, root=tmp_path, source_prefix="in")
        _login(app)

        minted = client.post(
            f"/api/v1/projects/{project.id}/uploads",
            json={"files": [{"path": "sub/a.png", "content_type": "image/png"}]},
        ).json()["uploads"][0]
        # The PUT is unauthenticated on purpose: the browser sends it with
        # no bearer token, exactly as it would against a cloud store.
        app.dependency_overrides.pop(get_current_user, None)

        put = client.put(minted["url"], content=_PIXEL, headers=minted["headers"])

        assert put.status_code == 204, put.text
        assert (tmp_path / "in" / "sub" / "a.png").read_bytes() == _PIXEL

    def test_403_for_a_non_owner(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        project = _seed(sessionmaker, root=tmp_path, role=ProjectRole.ANNOTATOR)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/uploads", json={"files": [{"path": "a.jpg"}]}
        )
        assert response.status_code == 403

    def test_409_without_a_source_connector(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        project = _seed(sessionmaker, root=None)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/uploads", json={"files": [{"path": "a.jpg"}]}
        )
        assert response.status_code == 409

    def test_409_when_the_connector_cannot_sign_writes(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        project = _seed(sessionmaker, root=tmp_path, connector_type=ConnectorType.S3)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/uploads", json={"files": [{"path": "a.jpg"}]}
        )
        assert response.status_code == 409
        assert "s3" in response.json()["detail"]

    @pytest.mark.parametrize("bad", ["../a.jpg", "/a.jpg", "x/../../a.jpg"])
    def test_422_for_an_escaping_path_and_nothing_is_minted(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
        bad: str,
    ) -> None:
        project = _seed(sessionmaker, root=tmp_path)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/uploads",
            json={"files": [{"path": "ok.jpg"}, {"path": bad}]},
        )
        assert response.status_code == 422

    def test_422_for_duplicate_paths(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        project = _seed(sessionmaker, root=tmp_path)
        _login(app)

        response = client.post(
            f"/api/v1/projects/{project.id}/uploads",
            json={"files": [{"path": "a.jpg"}, {"path": "./a.jpg"}]},
        )
        assert response.status_code == 422

    def test_422_for_an_empty_file_list(
        self,
        app: FastAPI,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        tmp_path: Path,
    ) -> None:
        project = _seed(sessionmaker, root=tmp_path)
        _login(app)

        response = client.post(f"/api/v1/projects/{project.id}/uploads", json={"files": []})
        assert response.status_code == 422

    def test_401_when_anonymous(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        project = _seed(sessionmaker, root=tmp_path)
        response = client.post(
            f"/api/v1/projects/{project.id}/uploads", json={"files": [{"path": "a.jpg"}]}
        )
        assert response.status_code == 401
