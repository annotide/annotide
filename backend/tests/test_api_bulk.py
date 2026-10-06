"""Tests for `POST /projects/{id}/items/bulk` (WF-8) and `services/bulk.py`.

Reuses the SQLite-per-test infrastructure and row helpers of
`tests/test_api_annotations.py`, since approval needs the same annotation,
task, comment and notification tables.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import CurrentUser
from app.models import (
    Annotation,
    AnnotationStatus,
    AuditEvent,
    Comment,
    Item,
    ItemStatus,
    ProjectRole,
    Task,
    TaskStatus,
    TaskType,
)
from tests import test_api_annotations as _shared
from tests.test_api_annotations import (
    _add_annotation,
    _add_item,
    _add_member,
    _add_task,
    _make_user,
    _seed_project,
    _set_workflow,
    _sign_in,
    _tasks_for_item,
)

# The fixtures of the annotation tests, re-exported so pytest finds them here.
app = _shared.app
client = _shared.client
sessionmaker = _shared.sessionmaker

_DEADLINE = "2030-06-01T12:00:00Z"


async def _item_row(sessionmaker: async_sessionmaker[AsyncSession], item_id: UUID) -> Item:
    async with sessionmaker() as session:
        item = await session.get(Item, item_id)
        assert item is not None
        return item


async def _owner(
    app: FastAPI,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    role: ProjectRole = ProjectRole.OWNER,
) -> tuple[CurrentUser, UUID, UUID, UUID]:
    """Seed a project, add a member with `role`, sign them in.

    Returns (user, connector_id, project_id, schema_version_id).
    """
    org_id, connector_id, project_id, schema_version_id = await _seed_project(sessionmaker)
    user = _make_user(org_id)
    await _add_member(sessionmaker, project_id, user.id, role)
    _sign_in(app, user)
    return user, connector_id, project_id, schema_version_id


class TestBulkAccess:
    async def test_annotator_forbidden(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, connector_id, project_id, _ = await _owner(app, sessionmaker, role=ProjectRole.ANNOTATOR)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)

        response = client.post(
            f"/api/v1/projects/{project_id}/items/bulk",
            json={"action": "tag", "item_ids": [str(item_id)], "add": ["x"]},
        )

        assert response.status_code == 403

    async def test_unknown_action_is_a_validation_error(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, _, project_id, _ = await _owner(app, sessionmaker)

        response = client.post(
            f"/api/v1/projects/{project_id}/items/bulk",
            json={"action": "explode", "item_ids": [str(uuid4())]},
        )

        assert response.status_code == 422

    async def test_items_from_elsewhere_are_skipped_not_found(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, connector_id, project_id, _ = await _owner(app, sessionmaker)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        stranger = uuid4()

        response = client.post(
            f"/api/v1/projects/{project_id}/items/bulk",
            json={"action": "tag", "item_ids": [str(item_id), str(stranger)], "add": ["x"]},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["applied"] == 1
        assert body["skipped"] == [
            {"item_id": str(stranger), "reason": "not found in this project"}
        ]

    async def test_request_is_audited(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        user, connector_id, project_id, _ = await _owner(app, sessionmaker)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)

        client.post(
            f"/api/v1/projects/{project_id}/items/bulk",
            json={"action": "tag", "item_ids": [str(item_id)], "add": ["x"]},
        )

        async with sessionmaker() as session:
            event = await session.scalar(
                select(AuditEvent).where(AuditEvent.action == "item.bulk.tag")
            )
        assert event is not None
        assert event.actor_id == user.id
        assert event.after == {"requested": 1, "applied": 1, "skipped": 0}


class TestBulkTag:
    async def test_adds_and_removes_tags_sorted_and_unique(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, connector_id, project_id, _ = await _owner(app, sessionmaker)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        url = f"/api/v1/projects/{project_id}/items/bulk"

        response = client.post(
            url, json={"action": "tag", "item_ids": [str(item_id)], "add": ["night", "blurry"]}
        )
        assert response.status_code == 200
        assert response.json()["applied"] == 1
        assert (await _item_row(sessionmaker, item_id)).meta["tags"] == ["blurry", "night"]

        response = client.post(
            url,
            json={
                "action": "tag",
                "item_ids": [str(item_id)],
                "add": ["night", "rain"],
                "remove": ["blurry"],
            },
        )
        assert response.json()["applied"] == 1
        assert (await _item_row(sessionmaker, item_id)).meta["tags"] == ["night", "rain"]

    async def test_the_item_list_filters_by_exact_tag(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, connector_id, project_id, _ = await _owner(app, sessionmaker)
        night = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        other = await _add_item(
            sessionmaker, project_id=project_id, connector_id=connector_id, path="images/b.jpg"
        )
        url = f"/api/v1/projects/{project_id}/items"
        bulk = f"/api/v1/projects/{project_id}/items/bulk"
        client.post(bulk, json={"action": "tag", "item_ids": [str(night)], "add": ["night_1"]})
        client.post(bulk, json={"action": "tag", "item_ids": [str(other)], "add": ["nightX1"]})

        def ids(tag: str) -> set[str]:
            response = client.get(url, params={"tag": tag})
            assert response.status_code == 200
            return {row["id"] for row in response.json()["items"]}

        assert ids("night_1") == {str(night)}  # `_` is literal, not a LIKE wildcard
        assert ids("night") == set()  # whole tags only, no substring match

    async def test_unchanged_tags_are_reported_as_skipped(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, connector_id, project_id, _ = await _owner(app, sessionmaker)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        url = f"/api/v1/projects/{project_id}/items/bulk"
        client.post(url, json={"action": "tag", "item_ids": [str(item_id)], "add": ["a"]})

        response = client.post(
            url, json={"action": "tag", "item_ids": [str(item_id)], "add": ["a"]}
        )

        assert response.json()["applied"] == 0
        assert response.json()["skipped"][0]["reason"] == "tags unchanged"

    async def test_tag_with_whitespace_is_rejected(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, connector_id, project_id, _ = await _owner(app, sessionmaker)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)

        response = client.post(
            f"/api/v1/projects/{project_id}/items/bulk",
            json={"action": "tag", "item_ids": [str(item_id)], "add": ["two words"]},
        )

        assert response.status_code == 422


class TestBulkAssign:
    async def test_opens_annotate_tasks_for_fresh_items_with_priority_and_deadline(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, connector_id, project_id, _ = await _owner(app, sessionmaker)
        assignee = uuid4()
        await _add_member(sessionmaker, project_id, assignee, ProjectRole.ANNOTATOR)
        item_a = await _add_item(
            sessionmaker, project_id=project_id, connector_id=connector_id, status=ItemStatus.NEW
        )
        item_b = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            path="images/b.jpg",
            status=ItemStatus.APPROVED,
        )

        response = client.post(
            f"/api/v1/projects/{project_id}/items/bulk",
            json={
                "action": "assign",
                "item_ids": [str(item_a), str(item_b)],
                "assignee_id": str(assignee),
                "priority": 5,
                "deadline": _DEADLINE,
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["applied"] == 1
        assert body["skipped"] == [
            {"item_id": str(item_b), "reason": "status 'approved' has no annotate work"}
        ]
        tasks = await _tasks_for_item(sessionmaker, item_a, TaskType.ANNOTATE)
        assert len(tasks) == 1
        assert tasks[0].status is TaskStatus.OPEN
        assert tasks[0].assignee_id == assignee
        assert tasks[0].priority == 5
        assert tasks[0].deadline is not None
        assert tasks[0].deadline.replace(tzinfo=UTC) == datetime(2030, 6, 1, 12, tzinfo=UTC)

    async def test_updates_only_the_given_fields_on_an_open_task(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, connector_id, project_id, _ = await _owner(app, sessionmaker)
        previous_assignee = uuid4()
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        await _add_task(
            sessionmaker,
            project_id=project_id,
            item_id=item_id,
            status=TaskStatus.OPEN,
            assignee_id=previous_assignee,
            priority=1,
        )

        response = client.post(
            f"/api/v1/projects/{project_id}/items/bulk",
            json={"action": "assign", "item_ids": [str(item_id)], "priority": 9},
        )

        assert response.json()["applied"] == 1
        tasks = await _tasks_for_item(sessionmaker, item_id, TaskType.ANNOTATE)
        assert len(tasks) == 1
        assert tasks[0].priority == 9
        assert tasks[0].assignee_id == previous_assignee  # not in the body → untouched

    async def test_null_assignee_returns_an_open_task_to_the_shared_queue(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, connector_id, project_id, _ = await _owner(app, sessionmaker)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        await _add_task(
            sessionmaker,
            project_id=project_id,
            item_id=item_id,
            status=TaskStatus.OPEN,
            assignee_id=uuid4(),
        )

        response = client.post(
            f"/api/v1/projects/{project_id}/items/bulk",
            json={"action": "assign", "item_ids": [str(item_id)], "assignee_id": None},
        )

        assert response.json()["applied"] == 1
        tasks = await _tasks_for_item(sessionmaker, item_id, TaskType.ANNOTATE)
        assert tasks[0].assignee_id is None

    async def test_non_member_assignee_is_rejected(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, connector_id, project_id, _ = await _owner(app, sessionmaker)
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)

        response = client.post(
            f"/api/v1/projects/{project_id}/items/bulk",
            json={"action": "assign", "item_ids": [str(item_id)], "assignee_id": str(uuid4())},
        )

        assert response.status_code == 422
        assert await _tasks_for_item(sessionmaker, item_id, TaskType.ANNOTATE) == []

    async def test_in_progress_task_is_not_reassigned_but_can_be_reprioritised(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, connector_id, project_id, _ = await _owner(app, sessionmaker)
        holder = uuid4()
        item_id = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        await _add_task(
            sessionmaker,
            project_id=project_id,
            item_id=item_id,
            status=TaskStatus.IN_PROGRESS,
            assignee_id=holder,
            locked_by_id=holder,
        )
        url = f"/api/v1/projects/{project_id}/items/bulk"
        other = uuid4()
        await _add_member(sessionmaker, project_id, other, ProjectRole.ANNOTATOR)

        response = client.post(
            url, json={"action": "assign", "item_ids": [str(item_id)], "assignee_id": str(other)}
        )
        assert response.json()["applied"] == 0
        assert "in progress" in response.json()["skipped"][0]["reason"]

        response = client.post(
            url, json={"action": "assign", "item_ids": [str(item_id)], "priority": 3}
        )
        assert response.json()["applied"] == 1
        tasks = await _tasks_for_item(sessionmaker, item_id, TaskType.ANNOTATE)
        assert tasks[0].priority == 3
        assert tasks[0].assignee_id == holder

    async def test_review_type_opens_review_tasks_only_for_submitted_items(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, connector_id, project_id, _ = await _owner(app, sessionmaker)
        reviewer = uuid4()
        await _add_member(sessionmaker, project_id, reviewer, ProjectRole.REVIEWER)
        submitted = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            status=ItemStatus.SUBMITTED,
        )
        fresh = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            path="images/b.jpg",
            status=ItemStatus.NEW,
        )

        response = client.post(
            f"/api/v1/projects/{project_id}/items/bulk",
            json={
                "action": "assign",
                "type": "review",
                "item_ids": [str(submitted), str(fresh)],
                "assignee_id": str(reviewer),
            },
        )

        assert response.json()["applied"] == 1
        assert response.json()["skipped"][0]["item_id"] == str(fresh)
        tasks = await _tasks_for_item(sessionmaker, submitted, TaskType.REVIEW)
        assert len(tasks) == 1
        assert tasks[0].assignee_id == reviewer
        assert await _tasks_for_item(sessionmaker, fresh, TaskType.REVIEW) == []


class TestBulkReturn:
    async def test_returns_held_tasks_to_the_queue_and_skips_idle_items(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, connector_id, project_id, _ = await _owner(app, sessionmaker)
        holder = uuid4()
        held = await _add_item(sessionmaker, project_id=project_id, connector_id=connector_id)
        idle = await _add_item(
            sessionmaker, project_id=project_id, connector_id=connector_id, path="images/b.jpg"
        )
        await _add_task(
            sessionmaker,
            project_id=project_id,
            item_id=held,
            status=TaskStatus.IN_PROGRESS,
            assignee_id=holder,
            locked_by_id=holder,
        )
        await _add_task(sessionmaker, project_id=project_id, item_id=idle, status=TaskStatus.OPEN)

        response = client.post(
            f"/api/v1/projects/{project_id}/items/bulk",
            json={"action": "return", "item_ids": [str(held), str(idle)]},
        )

        assert response.status_code == 200
        assert response.json()["applied"] == 1
        assert response.json()["skipped"] == [
            {"item_id": str(idle), "reason": "no task in progress"}
        ]
        tasks = await _tasks_for_item(sessionmaker, held, TaskType.ANNOTATE)
        assert tasks[0].status is TaskStatus.OPEN
        assert tasks[0].locked_by_id is None
        assert tasks[0].locked_until is None
        assert tasks[0].assignee_id is None


class TestBulkApprove:
    async def test_approves_submitted_items_and_skips_the_rest(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, connector_id, project_id, schema_version_id = await _owner(
            app, sessionmaker, role=ProjectRole.REVIEWER
        )
        author = uuid4()
        submitted = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            status=ItemStatus.SUBMITTED,
        )
        review_task_id = await _add_task(
            sessionmaker, project_id=project_id, item_id=submitted, task_type=TaskType.REVIEW
        )
        annotation_id = await _add_annotation(
            sessionmaker,
            item_id=submitted,
            schema_version_id=schema_version_id,
            author_user_id=author,
            status=AnnotationStatus.SUBMITTED,
        )
        # Status says submitted but the latest version is a draft: nothing to approve.
        half = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            path="images/b.jpg",
            status=ItemStatus.SUBMITTED,
        )
        await _add_annotation(
            sessionmaker,
            item_id=half,
            schema_version_id=schema_version_id,
            author_user_id=author,
            status=AnnotationStatus.DRAFT,
        )
        fresh = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            path="images/c.jpg",
            status=ItemStatus.NEW,
        )

        response = client.post(
            f"/api/v1/projects/{project_id}/items/bulk",
            json={
                "action": "approve",
                "item_ids": [str(submitted), str(half), str(fresh)],
                "comment": "Looks good",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["applied"] == 1
        assert {s["item_id"]: s["reason"] for s in body["skipped"]} == {
            str(half): "no submitted version to approve",
            str(fresh): "status 'new' is not awaiting review",
        }
        assert (await _item_row(sessionmaker, submitted)).status is ItemStatus.APPROVED
        async with sessionmaker() as session:
            annotation = await session.get(Annotation, annotation_id)
            assert annotation is not None
            assert annotation.status is AnnotationStatus.APPROVED
            review_task = await session.get(Task, review_task_id)
        assert review_task is not None
        assert review_task.status is TaskStatus.DONE

    async def test_rejects_with_the_shared_comment_and_requires_one(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        _, connector_id, project_id, schema_version_id = await _owner(
            app, sessionmaker, role=ProjectRole.REVIEWER
        )
        submitted = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            status=ItemStatus.SUBMITTED,
        )
        await _add_task(
            sessionmaker, project_id=project_id, item_id=submitted, task_type=TaskType.REVIEW
        )
        annotation_id = await _add_annotation(
            sessionmaker,
            item_id=submitted,
            schema_version_id=schema_version_id,
            author_user_id=uuid4(),
            status=AnnotationStatus.SUBMITTED,
        )
        fresh = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            path="images/c.jpg",
            status=ItemStatus.NEW,
        )
        url = f"/api/v1/projects/{project_id}/items/bulk"

        missing = client.post(url, json={"action": "reject", "item_ids": [str(submitted)]})
        assert missing.status_code == 422

        response = client.post(
            url,
            json={
                "action": "reject",
                "item_ids": [str(submitted), str(fresh)],
                "comment": "Boxes too loose",
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["applied"] == 1
        assert body["skipped"] == [
            {"item_id": str(fresh), "reason": "status 'new' is not awaiting review"}
        ]
        async with sessionmaker() as session:
            annotation = await session.get(Annotation, annotation_id)
            assert annotation is not None
            assert annotation.status is AnnotationStatus.REJECTED
            comments = list(
                await session.scalars(select(Comment).where(Comment.item_id == submitted))
            )
        assert [c.body for c in comments] == ["Boxes too loose"]

    async def test_self_review_is_skipped_when_the_project_forbids_it(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        user, connector_id, project_id, schema_version_id = await _owner(app, sessionmaker)
        await _set_workflow(sessionmaker, project_id, {"allow_self_review": False})
        own = await _add_item(
            sessionmaker,
            project_id=project_id,
            connector_id=connector_id,
            status=ItemStatus.SUBMITTED,
        )
        await _add_annotation(
            sessionmaker,
            item_id=own,
            schema_version_id=schema_version_id,
            author_user_id=user.id,
            status=AnnotationStatus.SUBMITTED,
        )

        response = client.post(
            f"/api/v1/projects/{project_id}/items/bulk",
            json={"action": "approve", "item_ids": [str(own)]},
        )

        assert response.status_code == 200
        assert response.json()["applied"] == 0
        assert "own annotation" in response.json()["skipped"][0]["reason"]
        assert (await _item_row(sessionmaker, own)).status is ItemStatus.SUBMITTED
