"""`services/correction_metrics.py` (ML-5).

Shape-level logic is tested directly; the database walk reuses the worker
test fixtures (SQLite file, local connector) to seed drafts and human
versions.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Annotation, AnnotationSource, AnnotationStatus, Item, ItemStatus
from app.schemas import AnnotationResult
from app.schemas.annotation import BBoxShape
from app.services.correction_metrics import (
    _Tally,
    bbox_iou,
    compare_shapes,
    correction_metrics,
)
from tests import test_worker as _shared
from tests.test_worker import (
    USER_ID,
    Fixture,
    _bbox,
    _result,
    _run,
    _seed_model_version,
    _seed_project,
)

engine = _shared.engine
sessionmaker = _shared.sessionmaker


class TestBboxIou:
    def test_identical_and_disjoint(self) -> None:
        a = BBoxShape(id=uuid.uuid4(), **{"class": "car"}, bbox=(0, 0, 10, 10))
        b = BBoxShape(id=uuid.uuid4(), **{"class": "car"}, bbox=(20, 20, 30, 30))
        assert bbox_iou(a, a) == 1.0
        assert bbox_iou(a, b) == 0.0

    def test_partial_overlap(self) -> None:
        a = BBoxShape(id=uuid.uuid4(), **{"class": "car"}, bbox=(0, 0, 10, 10))
        b = BBoxShape(id=uuid.uuid4(), **{"class": "car"}, bbox=(5, 0, 15, 10))
        # 50 / (100 + 100 - 50)
        assert round(bbox_iou(a, b), 4) == 0.3333


class TestCompareShapes:
    def test_classifies_every_shape_fate(self) -> None:
        kept, adjusted, relabeled, deleted = (uuid.uuid4() for _ in range(4))
        draft = _result(
            {**_bbox(), "id": str(kept)},
            {**_bbox(coords=(0, 0, 10, 10)), "id": str(adjusted)},
            {**_bbox(), "id": str(relabeled)},
            {**_bbox(), "id": str(deleted)},
        )
        final = _result(
            {**_bbox(), "id": str(kept)},
            {**_bbox(coords=(5, 0, 15, 10)), "id": str(adjusted)},
            {**_bbox(cls="truck"), "id": str(relabeled)},
            _bbox(cls="person"),  # added by the human
        )
        tallies: dict[str, _Tally] = defaultdict(_Tally)

        unchanged = compare_shapes(draft, final, tallies)

        assert unchanged is False
        car = tallies["car"]
        assert (car.model, car.kept, car.adjusted, car.relabeled, car.deleted, car.added) == (
            4,
            1,
            1,
            1,
            1,
            0,
        )
        assert car.precision == 0.5  # kept + adjusted over model
        assert car.recall == 1.0  # the human's final "car" shapes are exactly kept + adjusted
        assert car.mean_iou == 0.3333
        assert tallies["person"].added == 1
        assert tallies["person"].precision is None
        assert tallies["person"].recall == 0.0
        # The relabeled shape is a final "truck" the model did not draw as one.
        assert tallies["truck"].model == 0
        assert tallies["truck"].recall == 0.0

    def test_identical_versions_are_an_unchanged_acceptance(self) -> None:
        shape = _bbox()
        tallies: dict[str, _Tally] = defaultdict(_Tally)
        assert compare_shapes(_result(shape), _result(shape), tallies) is True
        assert tallies["car"].kept == 1

    def test_confidence_and_attributes_do_not_count_as_geometry(self) -> None:
        shape = _bbox()
        draft = _result({**shape, "confidence": 0.7})
        final = _result({**shape, "attributes": {"colour": "red"}})
        tallies: dict[str, _Tally] = defaultdict(_Tally)
        assert compare_shapes(draft, final, tallies) is True


def _seed_item(sessionmaker: async_sessionmaker[AsyncSession], fx: Fixture, path: str) -> Item:
    async def _create() -> Item:
        async with sessionmaker() as session:
            item = Item(
                project_id=fx.project.id,
                connector_id=fx.connector.id,
                path=path,
                media_type="image",
                size_bytes=1,
                meta={},
                status=ItemStatus.PRELABELED,
            )
            session.add(item)
            await session.commit()
            await session.refresh(item)
            return item

    return _run(_create())


def _add_version(
    sessionmaker: async_sessionmaker[AsyncSession],
    fx: Fixture,
    item: Item,
    *,
    version: int,
    result: AnnotationResult,
    model_version_id: uuid.UUID | None = None,
    status: AnnotationStatus = AnnotationStatus.SUBMITTED,
) -> None:
    async def _create() -> None:
        async with sessionmaker() as session:
            session.add(
                Annotation(
                    item_id=item.id,
                    version=version,
                    label_schema_version_id=fx.schema_version_id,
                    author_user_id=None if model_version_id else USER_ID,
                    author_model_version_id=model_version_id,
                    source=AnnotationSource.MODEL if model_version_id else AnnotationSource.HUMAN,
                    status=status,
                    result=result.model_dump(mode="json", by_alias=True),
                )
            )
            await session.commit()

    _run(_create())


class TestCorrectionMetrics:
    def test_walks_drafts_and_their_final_human_versions(
        self, sessionmaker: async_sessionmaker[AsyncSession], tmp_path: Path
    ) -> None:
        fx = _seed_project(sessionmaker, tmp_path)
        version = _seed_model_version(sessionmaker)
        shape_a, shape_b = _bbox(), _bbox(coords=(0, 0, 10, 10))

        accepted = _seed_item(sessionmaker, fx, "a.png")
        _add_version(
            sessionmaker,
            fx,
            accepted,
            version=1,
            result=_result(shape_a),
            model_version_id=version.id,
            status=AnnotationStatus.DRAFT,
        )
        _add_version(sessionmaker, fx, accepted, version=2, result=_result(shape_a))

        corrected = _seed_item(sessionmaker, fx, "b.png")
        _add_version(
            sessionmaker,
            fx,
            corrected,
            version=1,
            result=_result(shape_a, shape_b),
            model_version_id=version.id,
            status=AnnotationStatus.DRAFT,
        )
        # A draft the human saved but never submitted is not final ...
        _add_version(
            sessionmaker,
            fx,
            corrected,
            version=2,
            result=_result(),
            status=AnnotationStatus.DRAFT,
        )
        # ... the approved version is: one box deleted, one kept, one added.
        _add_version(
            sessionmaker,
            fx,
            corrected,
            version=3,
            result=_result(shape_a, _bbox(cls="person")),
            status=AnnotationStatus.APPROVED,
        )

        pending = _seed_item(sessionmaker, fx, "c.png")
        _add_version(
            sessionmaker,
            fx,
            pending,
            version=1,
            result=_result(shape_a),
            model_version_id=version.id,
            status=AnnotationStatus.DRAFT,
        )

        async def _compute(project_id: uuid.UUID | None) -> Any:
            async with sessionmaker() as session:
                return await correction_metrics(
                    session, version_id=version.id, project_id=project_id
                )

        metrics = _run(_compute(None))

        assert metrics.items_predicted == 3
        assert metrics.items_corrected == 2
        assert metrics.items_pending == 1
        assert metrics.items_accepted_unchanged == 1
        assert metrics.shapes.model_dump() == {
            "model": 3,
            "kept": 2,
            "adjusted": 0,
            "relabeled": 0,
            "deleted": 1,
            "added": 1,
        }
        assert metrics.precision == round(2 / 3, 4)
        assert metrics.recall == round(2 / 3, 4)
        assert metrics.mean_iou_adjusted is None
        assert [(c.name, c.model, c.added) for c in metrics.classes] == [
            ("car", 3, 0),
            ("person", 0, 1),
        ]

        # Scoped to another project: nothing.
        empty = _run(_compute(uuid.uuid4()))
        assert empty.items_predicted == 0
        assert empty.precision is None
