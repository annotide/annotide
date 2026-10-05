"""Parallel annotate tasks on one item: consensus and gold (QA-1, QA-4).

CONTRACTS.md *task* ("Parallel annotate tasks") and *annotation* (`kind`,
blind annotation). Same SQLite fixtures as `tests/test_api_annotations.py`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentUser
from app.models import (
    Annotation,
    AnnotationKind,
    AnnotationStatus,
    Item,
    ItemStatus,
    OutboxEvent,
    ProjectRole,
    Task,
    TaskStatus,
    TaskType,
)
from app.services.annotations import latest_version
from tests import test_api_annotations as _shared
from tests.test_api_annotations import (
    _add_annotation,
    _add_item,
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

# The fixtures of the annotation tests, re-exported so pytest finds them here.
app = _shared.app
client = _shared.client
sessionmaker = _shared.sessionmaker

Maker = async_sessionmaker[AsyncSession]


class _Project:
    def __init__(
        self, org_id: UUID, connector_id: UUID, project_id: UUID, schema_version_id: UUID
    ) -> None:
        self.org_id = org_id
        self.connector_id = connector_id
        self.id = project_id
        self.schema_version_id = schema_version_id

    async def member(self, sessionmaker: Maker, role: ProjectRole) -> CurrentUser:
        user = _make_user(self.org_id)
        await _add_member(sessionmaker, self.id, user.id, role)
        return user


async def _project(sessionmaker: Maker, workflow: dict[str, Any] | None = None) -> _Project:
    project = _Project(*await _seed_project(sessionmaker))
    if workflow is not None:
        await _set_workflow(sessionmaker, project.id, workflow)
    return project


async def _item(sessionmaker: Maker, item_id: UUID) -> Item:
    async with sessionmaker() as session:
        item = await session.get(Item, item_id)
        assert item is not None
        return item


def _save(
    client: TestClient,
    item_id: UUID,
    project: _Project,
    *,
    task_id: UUID | str | None = None,
    submit: bool = True,
) -> Any:
    body: dict[str, Any] = {
        "result": _result(_bbox()),
        "label_schema_version_id": str(project.schema_version_id),
        "submit": submit,
    }
    if task_id is not None:
        body["task_id"] = str(task_id)
    return client.post(f"/api/v1/items/{item_id}/annotations", json=body)


def _claim(client: TestClient, project: _Project) -> Any:
    return client.post(
        "/api/v1/tasks/next", params={"project_id": str(project.id), "type": "annotate"}
    )


class TestWorkflowValidation:
    async def test_consensus_bounds_and_review_requirement(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project = await _project(sessionmaker)
        _sign_in(app, await project.member(sessionmaker, ProjectRole.OWNER))
        url = f"/api/v1/projects/{project.id}"
        for workflow in (
            {"consensus_annotators": 0},
            {"consensus_annotators": 11},
            {"consensus_annotators": 3, "review": "none"},
        ):
            assert client.patch(url, json={"workflow": workflow}).status_code == 422
        ok = client.patch(url, json={"workflow": {"consensus_annotators": 3}})
        assert ok.status_code == 200
        assert ok.json()["workflow"]["consensus_annotators"] == 3


class TestConsensus:
    async def test_three_annotators_then_one_review(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project = await _project(sessionmaker, {"consensus_annotators": 3})
        owner = await project.member(sessionmaker, ProjectRole.OWNER)
        annotators = [await project.member(sessionmaker, ProjectRole.ANNOTATOR) for _ in range(4)]
        item_id = await _add_item(
            sessionmaker,
            project_id=project.id,
            connector_id=project.connector_id,
            status=ItemStatus.NEW,
        )

        _sign_in(app, owner)
        created = client.post(
            f"/api/v1/projects/{project.id}/tasks",
            json={"item_id": str(item_id), "type": "annotate"},
        )
        assert created.status_code == 201, created.text
        tasks = await _tasks_for_item(sessionmaker, item_id, TaskType.ANNOTATE)
        assert sorted(t.slot for t in tasks if t.slot is not None) == [0, 1, 2]
        assert all(t.assignee_id is None for t in tasks)

        slots: set[int] = set()
        for index, user in enumerate(annotators[:3]):
            _sign_in(app, user)
            claimed = _claim(client, project)
            assert claimed.status_code == 200, claimed.text
            slots.add(claimed.json()["slot"])
            saved = _save(client, item_id, project, task_id=claimed.json()["id"])
            assert saved.status_code == 201, saved.text
            assert saved.json()["kind"] == "consensus"
            assert saved.json()["task_id"] == claimed.json()["id"]

            item = await _item(sessionmaker, item_id)
            reviews = await _tasks_for_item(sessionmaker, item_id, TaskType.REVIEW)
            if index < 2:
                assert item.status is ItemStatus.ANNOTATING
                assert reviews == []
            else:
                assert item.status is ItemStatus.SUBMITTED
                assert len(reviews) == 1
        assert slots == {0, 1, 2}

        # The fourth annotator finds nothing left on this item.
        _sign_in(app, annotators[3])
        assert _claim(client, project).status_code == 204

    async def test_one_person_never_holds_two_slots(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project = await _project(sessionmaker, {"consensus_annotators": 2})
        annotator = await project.member(sessionmaker, ProjectRole.ANNOTATOR)
        item_id = await _add_item(
            sessionmaker, project_id=project.id, connector_id=project.connector_id
        )
        for _ in range(2):
            await _add_task(
                sessionmaker,
                project_id=project.id,
                item_id=item_id,
                status=TaskStatus.OPEN,
            )
        async with sessionmaker() as session:
            rows = list(await session.scalars(select(Task).where(Task.item_id == item_id)))
            for slot, row in enumerate(rows):
                row.slot = slot
            await session.commit()

        _sign_in(app, annotator)
        first = _claim(client, project)
        assert first.status_code == 200
        assert _save(client, item_id, project, task_id=first.json()["id"]).status_code == 201
        # Their slot is done; the other slot is someone else's to take.
        assert _claim(client, project).status_code == 204

    async def test_rejection_reopens_one_ordinary_task(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project = await _project(sessionmaker, {"consensus_annotators": 2})
        reviewer = await project.member(sessionmaker, ProjectRole.REVIEWER)
        item_id = await _add_item(
            sessionmaker,
            project_id=project.id,
            connector_id=project.connector_id,
            status=ItemStatus.SUBMITTED,
        )
        await _add_task(
            sessionmaker,
            project_id=project.id,
            item_id=item_id,
            task_type=TaskType.REVIEW,
            status=TaskStatus.OPEN,
        )
        primary = await _add_annotation(
            sessionmaker,
            item_id=item_id,
            schema_version_id=project.schema_version_id,
            author_user_id=reviewer.id,
        )
        _sign_in(app, reviewer)
        rejected = client.post(
            f"/api/v1/annotations/{primary}/review", json={"approve": False, "comment": "redo"}
        )
        assert rejected.status_code == 200, rejected.text
        live = [
            t
            for t in await _tasks_for_item(sessionmaker, item_id, TaskType.ANNOTATE)
            if t.status is TaskStatus.OPEN
        ]
        assert len(live) == 1
        assert live[0].slot is None


class TestTaskResolution:
    async def test_foreign_or_misplaced_task_id_is_409(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project = await _project(sessionmaker)
        me = await project.member(sessionmaker, ProjectRole.ANNOTATOR)
        other = await project.member(sessionmaker, ProjectRole.ANNOTATOR)
        item_a = await _add_item(
            sessionmaker, project_id=project.id, connector_id=project.connector_id, path="a.jpg"
        )
        item_b = await _add_item(
            sessionmaker, project_id=project.id, connector_id=project.connector_id, path="b.jpg"
        )
        theirs = await _add_task(
            sessionmaker,
            project_id=project.id,
            item_id=item_a,
            assignee_id=other.id,
            locked_by_id=other.id,
        )
        mine_on_b = await _add_task(
            sessionmaker,
            project_id=project.id,
            item_id=item_b,
            assignee_id=me.id,
            locked_by_id=me.id,
        )
        _sign_in(app, me)
        assert _save(client, item_a, project, task_id=theirs).status_code == 409
        assert _save(client, item_a, project, task_id=mine_on_b).status_code == 409


class TestGold:
    async def test_gold_submit_touches_nothing_but_its_task(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project = await _project(sessionmaker)
        annotator = await project.member(sessionmaker, ProjectRole.ANNOTATOR)
        item_id = await _add_item(
            sessionmaker,
            project_id=project.id,
            connector_id=project.connector_id,
            status=ItemStatus.APPROVED,
        )
        gold_task = await _add_task(
            sessionmaker,
            project_id=project.id,
            item_id=item_id,
            assignee_id=annotator.id,
            locked_by_id=annotator.id,
        )
        async with sessionmaker() as session:
            row = await session.get(Task, gold_task)
            assert row is not None
            row.gold = True
            await session.commit()
            outbox_before = await session.scalar(select(func.count()).select_from(OutboxEvent))

        _sign_in(app, annotator)
        saved = _save(client, item_id, project, task_id=gold_task)
        assert saved.status_code == 201, saved.text
        assert saved.json()["kind"] == "gold"

        item = await _item(sessionmaker, item_id)
        assert item.status is ItemStatus.APPROVED
        task = await _tasks_for_item(sessionmaker, item_id, TaskType.ANNOTATE)
        assert [t.status for t in task] == [TaskStatus.DONE]
        assert await _tasks_for_item(sessionmaker, item_id, TaskType.REVIEW) == []
        async with sessionmaker() as session:
            outbox_after = await session.scalar(select(func.count()).select_from(OutboxEvent))
            # A gold attempt is never published to the result connector.
            assert outbox_after == outbox_before
            assert await latest_version(session, item_id) is None


class TestBlindListing:
    async def test_consensus_annotator_sees_only_their_own_work(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project = await _project(sessionmaker, {"consensus_annotators": 2})
        owner = await project.member(sessionmaker, ProjectRole.OWNER)
        alice = await project.member(sessionmaker, ProjectRole.ANNOTATOR)
        bob = await project.member(sessionmaker, ProjectRole.ANNOTATOR)
        item_id = await _add_item(
            sessionmaker, project_id=project.id, connector_id=project.connector_id
        )
        # A prelabel-like primary draft that consensus annotators must not see.
        await _add_annotation(
            sessionmaker,
            item_id=item_id,
            schema_version_id=project.schema_version_id,
            author_user_id=owner.id,
            status=AnnotationStatus.DRAFT,
        )
        tasks: dict[UUID, UUID] = {}
        for slot, user in enumerate((alice, bob)):
            task_id = await _add_task(
                sessionmaker,
                project_id=project.id,
                item_id=item_id,
                assignee_id=user.id,
                locked_by_id=user.id,
            )
            async with sessionmaker() as session:
                row = await session.get(Task, task_id)
                assert row is not None
                row.slot = slot
                await session.commit()
            tasks[user.id] = task_id
            _sign_in(app, user)
            assert _save(client, item_id, project, task_id=task_id, submit=False).status_code == 201

        url = f"/api/v1/items/{item_id}/annotations"
        _sign_in(app, alice)
        seen = client.get(url).json()
        assert {(a["kind"], a["author_user_id"]) for a in seen} == {("consensus", str(alice.id))}

        _sign_in(app, owner)
        kinds = sorted(a["kind"] for a in client.get(url).json())
        assert kinds == ["consensus", "consensus", "primary"]

    async def test_without_a_live_task_primary_plus_own(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project = await _project(sessionmaker)
        owner = await project.member(sessionmaker, ProjectRole.OWNER)
        alice = await project.member(sessionmaker, ProjectRole.ANNOTATOR)
        bob = await project.member(sessionmaker, ProjectRole.ANNOTATOR)
        item_id = await _add_item(
            sessionmaker, project_id=project.id, connector_id=project.connector_id
        )
        primary = await _add_annotation(
            sessionmaker,
            item_id=item_id,
            schema_version_id=project.schema_version_id,
            author_user_id=owner.id,
        )
        own = await _add_annotation(
            sessionmaker,
            item_id=item_id,
            schema_version_id=project.schema_version_id,
            author_user_id=alice.id,
        )
        other = await _add_annotation(
            sessionmaker,
            item_id=item_id,
            schema_version_id=project.schema_version_id,
            author_user_id=bob.id,
        )
        async with sessionmaker() as session:
            for annotation_id in (own, other):
                row = await session.get(Annotation, annotation_id)
                assert row is not None
                row.kind = AnnotationKind.CONSENSUS
            await session.commit()

        _sign_in(app, alice)
        ids = {a["id"] for a in client.get(f"/api/v1/items/{item_id}/annotations").json()}
        assert ids == {str(primary), str(own)}


class TestPrimaryOnly:
    async def test_latest_version_and_review_ignore_consensus(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project = await _project(sessionmaker)
        reviewer = await project.member(sessionmaker, ProjectRole.REVIEWER)
        item_id = await _add_item(
            sessionmaker,
            project_id=project.id,
            connector_id=project.connector_id,
            status=ItemStatus.SUBMITTED,
        )
        primary = await _add_annotation(
            sessionmaker,
            item_id=item_id,
            schema_version_id=project.schema_version_id,
            author_user_id=reviewer.id,
        )
        newer = await _add_annotation(
            sessionmaker,
            item_id=item_id,
            schema_version_id=project.schema_version_id,
            author_user_id=reviewer.id,
        )
        async with sessionmaker() as session:
            row = await session.get(Annotation, newer)
            assert row is not None
            row.kind = AnnotationKind.CONSENSUS
            await session.commit()
            latest = await latest_version(session, item_id)
            assert latest is not None and latest.id == primary

        _sign_in(app, reviewer)
        response = client.post(f"/api/v1/annotations/{newer}/review", json={"approve": True})
        assert response.status_code == 409
