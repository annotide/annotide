"""Region split and region-task saves (IMG-6).

CONTRACTS.md REST `POST /items/{id}/split` and *task* ("Parallel annotate
tasks", Region). Same SQLite fixtures as `tests/test_api_annotations.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentUser
from app.models import (
    AuditEvent,
    Item,
    ItemStatus,
    MediaType,
    ProjectRole,
    Task,
    TaskStatus,
    TaskType,
)
from app.schemas.split import SplitGrid
from app.services.annotations import latest_version
from app.services.splitting import grid_regions
from tests import test_api_annotations as _shared
from tests.test_api_annotations import (
    _add_member,
    _add_task,
    _bbox,
    _make_user,
    _result,
    _seed_project,
    _set_workflow,
    _sign_in,
    _tasks_for_item,
)
from tests.test_parallel_tasks import _Project

app = _shared.app
client = _shared.client
sessionmaker = _shared.sessionmaker

Maker = async_sessionmaker[AsyncSession]
_DEADLINE = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)


async def _setup(
    sessionmaker: Maker, role: ProjectRole = ProjectRole.OWNER
) -> tuple[_Project, CurrentUser]:
    project = _Project(*await _seed_project(sessionmaker))
    user = _make_user(project.org_id)
    await _add_member(sessionmaker, project.id, user.id, role)
    return project, user


async def _image(
    sessionmaker: Maker,
    project: _Project,
    *,
    width: int | None = 100,
    height: int | None = 80,
    media_type: MediaType = MediaType.IMAGE,
    status: ItemStatus = ItemStatus.NEW,
    path: str = "images/big.tif",
) -> UUID:
    async with sessionmaker() as session:
        item = Item(
            project_id=project.id,
            connector_id=project.connector_id,
            path=path,
            media_type=media_type,
            size_bytes=100,
            width=width,
            height=height,
            meta={},
            status=status,
        )
        session.add(item)
        await session.commit()
        return item.id


def _split(client: TestClient, item_id: UUID, body: dict[str, Any]) -> Any:
    return client.post(f"/api/v1/items/{item_id}/split", json=body)


class TestGridRegions:
    def test_two_by_two_with_overlap_extends_inner_edges_only(self) -> None:
        cells = grid_regions(100, 80, SplitGrid(rows=2, cols=2, overlap_px=5))
        assert cells == [
            (0, 0, 55, 45),
            (45, 0, 100, 45),
            (0, 35, 55, 80),
            (45, 35, 100, 80),
        ]


class TestSplitEndpoint:
    async def test_grid_replaces_the_ordinary_task_and_inherits_urgency(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project, user = await _setup(sessionmaker)
        item_id = await _image(sessionmaker, project)
        ordinary = await _add_task(
            sessionmaker,
            project_id=project.id,
            item_id=item_id,
            status=TaskStatus.OPEN,
            priority=7,
            deadline=_DEADLINE,
        )
        _sign_in(app, user)

        response = _split(client, item_id, {"grid": {"rows": 2, "cols": 2}})
        assert response.status_code == 200, response.text
        regions = [task["region"] for task in response.json()["tasks"]]
        assert regions == [[0, 0, 50, 40], [50, 0, 100, 40], [0, 40, 50, 80], [50, 40, 100, 80]]
        assert {task["priority"] for task in response.json()["tasks"]} == {7}

        async with sessionmaker() as session:
            old = await session.get(Task, ordinary)
            assert old is not None and old.status is TaskStatus.CANCELLED
            audit = await session.scalar(
                select(AuditEvent).where(AuditEvent.action == "item.split")
            )
            assert audit is not None
        live = [
            t
            for t in await _tasks_for_item(sessionmaker, item_id, TaskType.ANNOTATE)
            if t.status is TaskStatus.OPEN
        ]
        assert len(live) == 4
        assert all(t.deadline is not None for t in live)

    async def test_explicit_regions_are_clipped(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project, user = await _setup(sessionmaker)
        item_id = await _image(sessionmaker, project)
        _sign_in(app, user)
        response = _split(client, item_id, {"regions": [[-10, -10, 60, 50], [40, 30, 500, 500]]})
        assert response.status_code == 200, response.text
        assert [t["region"] for t in response.json()["tasks"]] == [
            [0, 0, 60, 50],
            [40, 30, 100, 80],
        ]

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"grid": {"rows": 2, "cols": 2}, "regions": [[0, 0, 10, 10]]},
            {"grid": {"rows": 0, "cols": 2}},
            {"grid": {"rows": 17, "cols": 1}},
            {"regions": []},
            {"regions": [[200, 200, 300, 300]]},
        ],
    )
    async def test_bad_bodies_are_422(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker, body: dict[str, Any]
    ) -> None:
        project, user = await _setup(sessionmaker)
        item_id = await _image(sessionmaker, project)
        _sign_in(app, user)
        assert _split(client, item_id, body).status_code == 422

    async def test_conflicts(self, app: FastAPI, client: TestClient, sessionmaker: Maker) -> None:
        project, user = await _setup(sessionmaker)
        _sign_in(app, user)
        grid = {"grid": {"rows": 1, "cols": 2}}

        video = await _image(sessionmaker, project, media_type=MediaType.VIDEO, path="v.mp4")
        no_size = await _image(sessionmaker, project, width=None, height=None, path="n.png")
        approved = await _image(sessionmaker, project, status=ItemStatus.APPROVED, path="a.png")
        busy = await _image(sessionmaker, project, path="b.png")
        await _add_task(sessionmaker, project_id=project.id, item_id=busy)  # in_progress
        for item_id in (video, no_size, approved, busy):
            assert _split(client, item_id, grid).status_code == 409

        twice = await _image(sessionmaker, project, path="t.png")
        assert _split(client, twice, grid).status_code == 200
        assert _split(client, twice, grid).status_code == 409

        await _set_workflow(sessionmaker, project.id, {"consensus_annotators": 2})
        fresh = await _image(sessionmaker, project, path="f.png")
        assert _split(client, fresh, grid).status_code == 409

    async def test_annotators_may_not_split(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project, user = await _setup(sessionmaker, ProjectRole.ANNOTATOR)
        item_id = await _image(sessionmaker, project)
        _sign_in(app, user)
        assert _split(client, item_id, {"grid": {"rows": 1, "cols": 2}}).status_code == 403


class TestRegionSaves:
    async def _two_regions(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> tuple[_Project, UUID, list[UUID]]:
        project, user = await _setup(sessionmaker)
        item_id = await _image(sessionmaker, project)
        _sign_in(app, user)
        response = _split(client, item_id, {"grid": {"rows": 1, "cols": 2}})
        assert response.status_code == 200, response.text
        task_ids = [UUID(t["id"]) for t in response.json()["tasks"]]
        # Hand each region to the signed-in user, as a claim would.
        async with sessionmaker() as session:
            for task_id in task_ids:
                task = await session.get(Task, task_id)
                assert task is not None
                task.status = TaskStatus.IN_PROGRESS
                task.assignee_id = task.locked_by_id = user.id
            await session.commit()
        return project, item_id, task_ids

    def _save(
        self,
        client: TestClient,
        project: _Project,
        item_id: UUID,
        task_id: UUID,
        *shapes: dict[str, Any],
        classification: dict[str, Any] | None = None,
        submit: bool = False,
    ) -> Any:
        result = _result(*shapes)
        result["classification"] = classification or {}
        return client.post(
            f"/api/v1/items/{item_id}/annotations",
            json={
                "result": result,
                "label_schema_version_id": str(project.schema_version_id),
                "task_id": str(task_id),
                "submit": submit,
            },
        )

    async def test_saves_merge_per_region(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project, item_id, (left, right) = await self._two_regions(app, client, sessionmaker)
        left_box = _bbox(coords=(10, 10, 20, 20))
        right_box = _bbox(coords=(60, 10, 70, 20))

        assert self._save(client, project, item_id, left, left_box).status_code == 201
        assert self._save(client, project, item_id, right, right_box).status_code == 201
        # Re-saving the left region replaces only the left region's shapes.
        new_left = _bbox(coords=(5, 5, 15, 15))
        saved = self._save(
            client, project, item_id, left, new_left, classification={"weather": "rain"}
        )
        assert saved.status_code == 201, saved.text
        assert saved.json()["kind"] == "primary"

        async with sessionmaker() as session:
            latest = await latest_version(session, item_id)
            assert latest is not None
            ids = {shape["id"] for shape in latest.result["shapes"]}
            assert ids == {new_left["id"], right_box["id"]}
            assert latest.result["classification"] == {"weather": "rain"}

    async def test_out_of_region_shape_is_422_with_ids(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project, item_id, (left, _right) = await self._two_regions(app, client, sessionmaker)
        # Anchor exactly on the left region's x_max (50) is outside: half-open.
        on_edge = _bbox(coords=(45, 10, 55, 20))
        response = self._save(client, project, item_id, left, on_edge)
        assert response.status_code == 422
        assert on_edge["id"] in response.text

    async def test_item_submits_after_the_last_region(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project, item_id, (left, right) = await self._two_regions(app, client, sessionmaker)
        box = _bbox(coords=(10, 10, 20, 20))
        assert self._save(client, project, item_id, left, box, submit=True).status_code == 201
        async with sessionmaker() as session:
            item = await session.get(Item, item_id)
            assert item is not None and item.status is ItemStatus.ANNOTATING
        assert self._save(client, project, item_id, right, submit=True).status_code == 201
        async with sessionmaker() as session:
            item = await session.get(Item, item_id)
            assert item is not None and item.status is ItemStatus.SUBMITTED
        assert len(await _tasks_for_item(sessionmaker, item_id, TaskType.REVIEW)) == 1
