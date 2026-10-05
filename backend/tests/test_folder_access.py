"""Folder-level access (§4): `membership.path_prefixes` limits what a member sees."""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser, get_current_user
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import (
    Annotation,
    AnnotationKind,
    AnnotationSource,
    AnnotationStatus,
    Connector,
    ConnectorIdentity,
    ConnectorType,
    Item,
    ItemStatus,
    MediaType,
    Membership,
    Organization,
    Project,
    ProjectRole,
    Task,
    TaskStatus,
    TaskType,
    User,
)
from app.schemas.membership import MemberCreate, MemberUpdate
from app.services.repository import path_in_scope


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


class World:
    owner: CurrentUser
    member: CurrentUser
    project_id: UUID
    items: dict[str, UUID]


def _current(user: User) -> CurrentUser:
    return CurrentUser(
        id=user.id,
        organization_id=user.organization_id,
        email=user.email,
        is_superuser=False,
        is_service=False,
        scopes=frozenset(),
    )


@pytest.fixture
async def world(sessionmaker: async_sessionmaker[AsyncSession]) -> World:
    w = World()
    async with sessionmaker() as session:
        org = Organization(name="Acme", slug="acme")
        session.add(org)
        await session.flush()
        owner = User(organization_id=org.id, email="o@acme.example", display_name="Owner")
        member = User(organization_id=org.id, email="m@acme.example", display_name="Member")
        connector = Connector(
            organization_id=org.id,
            name="c",
            type=ConnectorType.LOCAL,
            identity_type=ConnectorIdentity.NONE,
            config={"root": "/tmp/x"},
        )
        session.add_all([owner, member, connector])
        await session.flush()
        project = Project(organization_id=org.id, name="P", workflow={}, settings={})
        session.add(project)
        await session.flush()
        session.add_all(
            [
                Membership(user_id=owner.id, project_id=project.id, role=ProjectRole.OWNER),
                Membership(
                    user_id=member.id,
                    project_id=project.id,
                    role=ProjectRole.REVIEWER,
                    path_prefixes=["site-a/"],
                ),
            ]
        )
        w.items = {}
        for path in ("site-a/1.jpg", "site-a/2.jpg", "site-b/1.jpg", "site-a-old/1.jpg"):
            item = Item(
                project_id=project.id,
                connector_id=connector.id,
                path=path,
                media_type=MediaType.IMAGE,
                size_bytes=1,
                meta={},
                status=ItemStatus.NEW,
            )
            session.add(item)
            await session.flush()
            # The highest priority sits outside the member's folder.
            priority = 100 if path.startswith("site-b") else 1
            session.add(
                Task(
                    project_id=project.id,
                    item_id=item.id,
                    type=TaskType.ANNOTATE,
                    status=TaskStatus.OPEN,
                    priority=priority,
                )
            )
            w.items[path] = item.id
        await session.commit()
        w.owner, w.member, w.project_id = _current(owner), _current(member), project.id
    return w


def _client(sessionmaker: async_sessionmaker[AsyncSession], user: CurrentUser) -> TestClient:
    app: FastAPI = create_app()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    app.dependency_overrides[get_session] = _get_session
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, raise_server_exceptions=False)


def _paths(client: TestClient, project_id: UUID) -> list[str]:
    body = client.get(f"/api/v1/projects/{project_id}/items").json()
    return sorted(row["path"] for row in body["items"])


def test_prefix_matching_is_literal() -> None:
    assert path_in_scope("site-a/1.jpg", ["site-a/"])
    assert not path_in_scope("site-a-old/1.jpg", ["site-a/"])
    assert path_in_scope("anything", None)


async def test_a_limited_member_sees_only_their_folder(
    sessionmaker: async_sessionmaker[AsyncSession], world: World
) -> None:
    member = _client(sessionmaker, world.member)
    assert _paths(member, world.project_id) == ["site-a/1.jpg", "site-a/2.jpg"]
    assert len(_paths(_client(sessionmaker, world.owner), world.project_id)) == 4

    outside = world.items["site-b/1.jpg"]
    for path in ("", "/annotations", "/comments", "/views"):
        assert member.get(f"/api/v1/items/{outside}{path}").status_code == 404, path
    assert member.get(f"/api/v1/items/{world.items['site-a/1.jpg']}").status_code == 200

    tasks = member.get(f"/api/v1/projects/{world.project_id}/tasks").json()["items"]
    assert len(tasks) == 2


async def test_the_queue_hands_out_only_tasks_in_the_folder(
    sessionmaker: async_sessionmaker[AsyncSession], world: World
) -> None:
    member = _client(sessionmaker, world.member)
    claimed = member.post(
        "/api/v1/tasks/next", params={"project_id": str(world.project_id), "type": "annotate"}
    )
    assert claimed.status_code == 200
    assert claimed.json()["item_id"] in {
        str(world.items["site-a/1.jpg"]),
        str(world.items["site-a/2.jpg"]),
    }


async def test_bulk_skips_items_outside_the_folder(
    sessionmaker: async_sessionmaker[AsyncSession], world: World
) -> None:
    member = _client(sessionmaker, world.member)
    response = member.post(
        f"/api/v1/projects/{world.project_id}/items/bulk",
        json={
            "action": "tag",
            "item_ids": [str(world.items["site-a/1.jpg"]), str(world.items["site-b/1.jpg"])],
            "add": ["checked"],
        },
    )
    assert response.status_code == 200, response.text
    skipped = {row["item_id"]: row["reason"] for row in response.json()["skipped"]}
    assert skipped == {str(world.items["site-b/1.jpg"]): "not found in this project"}


async def test_owners_edit_folders_and_owners_are_never_limited(
    sessionmaker: async_sessionmaker[AsyncSession], world: World
) -> None:
    owner = _client(sessionmaker, world.owner)
    url = f"/api/v1/projects/{world.project_id}/members/{world.member.id}"

    widened = owner.patch(url, json={"path_prefixes": ["site-a/", "site-b/"]})
    assert widened.status_code == 200
    assert widened.json()["path_prefixes"] == ["site-a/", "site-b/"]
    assert widened.json()["role"] == "reviewer"
    member = _client(sessionmaker, world.member)
    assert len(_paths(member, world.project_id)) == 3

    assert owner.patch(url, json={"role": "owner"}).status_code == 422
    assert owner.patch(url, json={"path_prefixes": None}).json()["path_prefixes"] is None
    assert len(_paths(member, world.project_id)) == 4


@pytest.mark.parametrize(
    ("prefixes", "message"),
    [([], "1-20 folders"), ([""], "1-512"), (["a/../b/"], "'..'")],
)
def test_bad_prefixes(prefixes: list[str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        MemberUpdate(path_prefixes=prefixes)


def test_an_owner_cannot_be_added_with_folders() -> None:
    with pytest.raises(ValueError, match="whole project"):
        MemberCreate(email="x@acme.example", role="owner", path_prefixes=["a/"])


async def test_views_and_exports_respect_folders(
    sessionmaker: async_sessionmaker[AsyncSession], world: World
) -> None:
    from app.models import Job, JobStatus, JobType

    async with sessionmaker() as session:
        item = await session.get(Item, world.items["site-a/1.jpg"])
        assert item is not None
        item.meta = {"views": [{"path": "site-a/1.txt"}, {"path": "site-b/secret.txt"}]}
        session.add(
            job := Job(
                project_id=world.project_id,
                type=JobType.EXPORT,
                status=JobStatus.SUCCEEDED,
                payload={},
                result={"blob_path": "exports/x/coco.zip"},
            )
        )
        await session.commit()
        job_id = job.id

    member = _client(sessionmaker, world.member)
    views = member.get(f"/api/v1/items/{world.items['site-a/1.jpg']}/views").json()
    assert [v["path"] for v in views] == ["site-a/1.txt"]
    assert member.get(f"/api/v1/jobs/{job_id}/download").status_code == 403


async def test_dashboard_and_quality_aggregates_respect_folders(
    sessionmaker: async_sessionmaker[AsyncSession], world: World
) -> None:
    # Two consensus versions on an item outside the member's folder: the owner
    # pools it, the folder-limited reviewer must not even see that it exists.
    async with sessionmaker() as session:
        for version, author in enumerate((world.owner.id, world.member.id), start=1):
            session.add(
                Annotation(
                    item_id=world.items["site-b/1.jpg"],
                    version=version,
                    author_user_id=author,
                    source=AnnotationSource.HUMAN,
                    kind=AnnotationKind.CONSENSUS,
                    label_schema_version_id=uuid4(),
                    result={
                        "schema_version": 1,
                        "media_type": "image",
                        "shapes": [],
                        "classification": {"weather": "sun"},
                    },
                    status=AnnotationStatus.SUBMITTED,
                )
            )
        await session.commit()

    base = f"/api/v1/projects/{world.project_id}"
    owner, member = _client(sessionmaker, world.owner), _client(sessionmaker, world.member)

    owner_stats = owner.get(f"{base}/stats").json()
    member_stats = member.get(f"{base}/stats").json()
    assert owner_stats["items"]["total"] == 4
    assert member_stats["items"]["total"] == 2
    assert owner_stats["tasks"]["annotate"]["open"] == 4
    assert member_stats["tasks"]["annotate"]["open"] == 2

    assert owner.get(f"{base}/agreement").json()["items"] == 1
    assert member.get(f"{base}/agreement").json()["items"] == 0
    assert member.get(f"{base}/quality/annotators").status_code == 200
