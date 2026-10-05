"""Sampled review (QA-7) and the gold-task rate (QA-4).

CONTRACTS.md *Project workflow JSON*: `review: sampled` with
`review_sample_rate`, and `gold_every`. Same SQLite fixtures as
`tests/test_api_annotations.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import (
    AnnotationStatus,
    Item,
    ItemStatus,
    ProjectRole,
    Task,
    TaskStatus,
    TaskType,
)
from app.services.item_flow import in_review_sample
from app.services.tasks import reap_expired_locks, return_to_queue
from tests import test_api_annotations as _shared
from tests.test_api_annotations import _add_annotation, _add_item, _add_task, _tasks_for_item
from tests.test_parallel_tasks import Maker, _claim, _item, _project, _save

app = _shared.app
client = _shared.client
sessionmaker = _shared.sessionmaker


class TestSampleRule:
    def test_deterministic_and_close_to_the_rate(self) -> None:
        ids = [uuid5(UUID(int=0), str(n)) for n in range(4000)]
        share = sum(in_review_sample(i, 0.25) for i in ids) / len(ids)
        assert abs(share - 0.25) < 0.03
        assert [in_review_sample(i, 0.25) for i in ids[:50]] == [
            in_review_sample(i, 0.25) for i in ids[:50]
        ]
        assert all(in_review_sample(i, 1.0) for i in ids[:50])


async def _items_on_both_sides(
    sessionmaker: Maker, project_id: UUID, connector_id: UUID
) -> tuple[UUID, UUID]:
    """One item inside the 50 % sample and one outside."""
    inside: UUID | None = None
    outside: UUID | None = None
    for n in range(40):
        item_id = await _add_item(
            sessionmaker, project_id=project_id, connector_id=connector_id, path=f"s/{n}.jpg"
        )
        if in_review_sample(item_id, 0.5):
            inside = inside or item_id
        else:
            outside = outside or item_id
        if inside and outside:
            return inside, outside
    raise AssertionError("no split found in 40 items")


class TestSampledReview:
    async def test_in_sample_goes_to_review_the_rest_approves(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project = await _project(sessionmaker, {"review": "sampled", "review_sample_rate": 0.5})
        annotator = await project.member(sessionmaker, ProjectRole.ANNOTATOR)
        inside, outside = await _items_on_both_sides(sessionmaker, project.id, project.connector_id)

        _shared._sign_in(app, annotator)
        for item_id in (inside, outside):
            assert _save(client, item_id, project).status_code == 201

        assert (await _item(sessionmaker, inside)).status is ItemStatus.SUBMITTED
        assert len(await _tasks_for_item(sessionmaker, inside, TaskType.REVIEW)) == 1
        assert (await _item(sessionmaker, outside)).status is ItemStatus.APPROVED
        assert await _tasks_for_item(sessionmaker, outside, TaskType.REVIEW) == []

    async def test_a_rejected_item_is_always_reviewed_again(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project = await _project(sessionmaker, {"review": "sampled", "review_sample_rate": 0.5})
        annotator = await project.member(sessionmaker, ProjectRole.ANNOTATOR)
        _, outside = await _items_on_both_sides(sessionmaker, project.id, project.connector_id)
        await _add_annotation(
            sessionmaker,
            item_id=outside,
            schema_version_id=project.schema_version_id,
            author_user_id=annotator.id,
            status=AnnotationStatus.REJECTED,
        )
        async with sessionmaker() as session:
            item = await session.get(Item, outside)
            assert item is not None
            item.status = ItemStatus.REJECTED
            await session.commit()

        _shared._sign_in(app, annotator)
        assert _save(client, outside, project).status_code == 201
        assert (await _item(sessionmaker, outside)).status is ItemStatus.SUBMITTED
        assert len(await _tasks_for_item(sessionmaker, outside, TaskType.REVIEW)) == 1

    async def test_consensus_cannot_be_sampled(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project = await _project(sessionmaker)
        _shared._sign_in(app, await project.member(sessionmaker, ProjectRole.OWNER))
        response = client.patch(
            f"/api/v1/projects/{project.id}",
            json={"workflow": {"review": "sampled", "consensus_annotators": 2}},
        )
        assert response.status_code == 422
        for rate in (0, 1.5):
            bad = client.patch(
                f"/api/v1/projects/{project.id}",
                json={"workflow": {"review": "sampled", "review_sample_rate": rate}},
            )
            assert bad.status_code == 422


class TestGoldRate:
    async def test_every_third_claim_is_a_gold_task_while_gold_items_last(
        self, app: FastAPI, client: TestClient, sessionmaker: Maker
    ) -> None:
        project = await _project(sessionmaker, {"gold_every": 3})
        annotator = await project.member(sessionmaker, ProjectRole.ANNOTATOR)
        gold_item = await _add_item(
            sessionmaker,
            project_id=project.id,
            connector_id=project.connector_id,
            path="gold.jpg",
            status=ItemStatus.APPROVED,
        )
        async with sessionmaker() as session:
            item = await session.get(Item, gold_item)
            assert item is not None
            item.meta = {"gold_annotation_id": str(uuid4())}
            await session.commit()
        for n in range(5):
            item_id = await _add_item(
                sessionmaker,
                project_id=project.id,
                connector_id=project.connector_id,
                path=f"work/{n}.jpg",
                status=ItemStatus.NEW,
            )
            await _add_task(
                sessionmaker, project_id=project.id, item_id=item_id, status=TaskStatus.OPEN
            )

        _shared._sign_in(app, annotator)
        handed: list[bool] = []
        for _ in range(5):
            claimed = _claim(client, project)
            assert claimed.status_code == 200, claimed.text
            handed.append(claimed.json()["gold"])
            assert (
                _save(
                    client, UUID(claimed.json()["item_id"]), project, task_id=claimed.json()["id"]
                )
            ).status_code == 201
        # Two ordinary tasks, then gold; the only gold item is used up after that.
        assert handed == [False, False, True, False, False]
        assert (await _item(sessionmaker, gold_item)).status is ItemStatus.APPROVED

    async def test_gold_tasks_keep_their_annotator_when_released_or_reaped(
        self, sessionmaker: Maker
    ) -> None:
        project = await _project(sessionmaker)
        user = uuid4()
        item_id = await _add_item(
            sessionmaker, project_id=project.id, connector_id=project.connector_id
        )
        past = datetime.now(UTC) - timedelta(minutes=5)
        async with sessionmaker() as session:
            reaped = Task(
                project_id=project.id,
                item_id=item_id,
                type=TaskType.ANNOTATE,
                status=TaskStatus.IN_PROGRESS,
                assignee_id=user,
                locked_by_id=user,
                locked_until=past,
                gold=True,
            )
            released = Task(
                project_id=project.id,
                item_id=item_id,
                type=TaskType.ANNOTATE,
                status=TaskStatus.IN_PROGRESS,
                assignee_id=user,
                locked_by_id=user,
                locked_until=datetime.now(UTC) + timedelta(minutes=5),
                gold=True,
            )
            session.add_all([reaped, released])
            await session.commit()
            return_to_queue(released)
            await session.commit()
            assert await reap_expired_locks(session) == 1
            rows = list(await session.scalars(select(Task).where(Task.item_id == item_id)))
        assert {(t.status, t.assignee_id) for t in rows} == {(TaskStatus.OPEN, user)}
