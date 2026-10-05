"""Companion views (§5 multimodal): settings, item meta, scan grouping, signed views."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser, get_current_user
from app.connectors.registry import build_connector
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import (
    Connector,
    ConnectorIdentity,
    ConnectorType,
    Item,
    MediaType,
    Membership,
    Organization,
    Project,
    ProjectRole,
)
from app.schemas.item import ItemCreate
from app.schemas.project import ProjectUpdate
from app.services.scanning import companion_views, merge_views, scan_source


@compiles(CITEXT, "sqlite")  # pragma: no cover - exercised implicitly
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    await engine.dispose()


def _always(_: str) -> bool:
    return True


class TestGrouping:
    def test_companions_attach_to_every_primary_beside_them(self) -> None:
        paths = ["a/1.jpg", "a/1.txt", "a/1.json", "a/2.jpg", "a/3.txt", "b/1.txt", "a/x.y/1.txt"]
        views = companion_views(paths, frozenset({".txt", ".json"}), primary=_always)
        assert views == {"a/1.jpg": ["a/1.json", "a/1.txt"]}

    def test_a_companion_extension_is_never_a_primary(self) -> None:
        views = companion_views(["n/1.txt", "n/1.json"], frozenset({".txt"}), primary=_always)
        assert views == {"n/1.json": ["n/1.txt"]}
        assert (
            companion_views(["n/1.txt", "n/1.bin"], frozenset({".txt"}), primary=lambda p: False)
            == {}
        )

    def test_merge_keeps_labels_and_order(self) -> None:
        meta = {"content_type": "image/jpeg", "views": [{"path": "a/1.txt", "label": "Caption"}]}
        assert merge_views(meta, ["a/1.json", "a/1.txt"]) == {
            "content_type": "image/jpeg",
            "views": [{"path": "a/1.txt", "label": "Caption"}, {"path": "a/1.json"}],
        }


class TestValidation:
    @pytest.mark.parametrize(
        ("value", "message"),
        [
            ([], "1-10 extensions"),
            ("txt", "1-10 extensions"),
            (["txt"], "must look like"),
            ([".TXT"], "must look like"),
        ],
    )
    def test_bad_companion_extensions(self, value: Any, message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            ProjectUpdate(settings={"companion_extensions": value})

    def test_companion_extensions_are_normalised(self) -> None:
        update = ProjectUpdate(settings={"companion_extensions": [".txt", ".md", ".txt"]})
        assert update.settings == {"companion_extensions": [".md", ".txt"]}

    @pytest.mark.parametrize(
        ("views", "message"),
        [
            ("x", "at most 10"),
            ([{"path": f"{i}.txt"} for i in range(11)], "at most 10"),
            ([{"path": ""}], "non-empty 'path'"),
            ([{"path": "a.txt", "label": "x" * 101}], "at most 100"),
            ([{"path": "a.txt", "url": "https://evil"}], "only 'path' and 'label'"),
            ([{"path": "a.txt"}, {"path": "a.txt"}], "listed twice"),
        ],
    )
    def test_bad_views(self, views: Any, message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            ItemCreate(
                connector_id=uuid4(),
                path="a.jpg",
                media_type="image",
                size_bytes=1,
                meta={"views": views},
            )


async def _project(
    sessionmaker: async_sessionmaker[AsyncSession], root: Path, settings: dict[str, Any]
) -> tuple[Project, Connector]:
    async with sessionmaker() as session:
        org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:6]}")
        session.add(org)
        await session.flush()
        connector = Connector(
            organization_id=org.id,
            name="local",
            type=ConnectorType.LOCAL,
            identity_type=ConnectorIdentity.NONE,
            config={"root": str(root)},
        )
        session.add(connector)
        await session.flush()
        project = Project(
            organization_id=org.id,
            name="P",
            source_connector_id=connector.id,
            source_prefix="set/",
            workflow={},
            settings=settings,
        )
        session.add(project)
        await session.commit()
        return project, connector


def _files(root: Path) -> None:
    (root / "set").mkdir(parents=True)
    for name in ("1.png", "1.txt", "2.png", "3.txt"):
        (root / "set" / name).write_bytes(b"x")


async def _scan(
    sessionmaker: async_sessionmaker[AsyncSession], project: Project, connector: Connector
) -> dict[str, Any]:
    storage = build_connector("local", dict(connector.config), None)
    async with sessionmaker() as session:
        result = await scan_source(
            session, project_id=project.id, connector=connector, storage=storage, prefix="set/"
        )
        await session.commit()
    return result.as_dict()


async def _items(sessionmaker: async_sessionmaker[AsyncSession]) -> dict[str, Item]:
    async with sessionmaker() as session:
        return {item.path: item for item in await session.scalars(select(Item))}


async def test_scan_turns_siblings_into_views(
    sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    _files(tmp_path)
    project, connector = await _project(sessionmaker, tmp_path, {"companion_extensions": [".txt"]})

    result = await _scan(sessionmaker, project, connector)

    items = await _items(sessionmaker)
    assert set(items) == {"set/1.png", "set/2.png", "set/3.txt"}  # 3.txt has no primary
    assert items["set/1.png"].meta["views"] == [{"path": "set/1.txt"}]
    assert "views" not in items["set/2.png"].meta
    assert result["companions"] == 1

    # A re-scan of unchanged files keeps the views and adds no items.
    await _scan(sessionmaker, project, connector)
    assert (await _items(sessionmaker))["set/1.png"].meta["views"] == [{"path": "set/1.txt"}]


async def test_without_the_setting_every_file_is_an_item(
    sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    _files(tmp_path)
    project, connector = await _project(sessionmaker, tmp_path, {})
    result = await _scan(sessionmaker, project, connector)
    assert set(await _items(sessionmaker)) == {"set/1.png", "set/1.txt", "set/2.png", "set/3.txt"}
    assert result["companions"] == 0


def _client(sessionmaker: async_sessionmaker[AsyncSession], user: CurrentUser) -> TestClient:
    app: FastAPI = create_app()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _get_session
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


async def test_views_are_signed_and_kept_inside_the_source_prefix(
    sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    project, connector = await _project(sessionmaker, tmp_path, {})
    user = CurrentUser(
        id=uuid4(),
        organization_id=project.organization_id,
        email="a@x.example",
        is_superuser=False,
        is_service=False,
        scopes=frozenset(),
    )
    async with sessionmaker() as session:
        session.add(Membership(user_id=user.id, project_id=project.id, role=ProjectRole.ANNOTATOR))
        item = Item(
            project_id=project.id,
            connector_id=connector.id,
            path="set/1.png",
            media_type=MediaType.IMAGE,
            size_bytes=1,
            meta={
                "views": [
                    {"path": "set/1.txt", "label": "Caption"},
                    {"path": "secret/payroll.csv"},
                    {"path": "set/../secret/payroll.csv"},
                    {"path": "set/1.unknown"},
                ]
            },
        )
        session.add(item)
        await session.commit()
        item_id: UUID = item.id

    response = _client(sessionmaker, user).get(f"/api/v1/items/{item_id}/views")

    assert response.status_code == 200
    views = response.json()
    assert [v["path"] for v in views] == ["set/1.txt", "set/1.unknown"]
    assert views[0]["label"] == "Caption"
    assert views[0]["media_type"] == "text"
    assert views[0]["url"]
    assert views[1]["media_type"] is None


async def test_views_need_membership(
    sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    project, connector = await _project(sessionmaker, tmp_path, {})
    async with sessionmaker() as session:
        item = Item(
            project_id=project.id,
            connector_id=connector.id,
            path="set/1.png",
            media_type=MediaType.IMAGE,
            size_bytes=1,
            meta={},
        )
        session.add(item)
        await session.commit()
    stranger = CurrentUser(
        id=uuid4(),
        organization_id=project.organization_id,
        email="b@x.example",
        is_superuser=False,
        is_service=False,
        scopes=frozenset(),
    )
    assert _client(sessionmaker, stranger).get(f"/api/v1/items/{item.id}/views").status_code == 403
