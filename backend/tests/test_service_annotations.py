"""Tests for annotation versioning, blob documents and the review diff."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from app.schemas import AnnotationResult
from app.services.annotations import (
    ANNOTIDE_WRITTEN,
    blob_path_for,
    build_blob_document,
    diff_shapes,
)

PROJECT_ID = UUID("11111111-1111-1111-1111-111111111111")
ITEM_ID = UUID("22222222-2222-2222-2222-222222222222")
SHAPE_A = UUID("aaaaaaaa-0000-0000-0000-000000000001")
SHAPE_B = UUID("bbbbbbbb-0000-0000-0000-000000000002")
SHAPE_C = UUID("cccccccc-0000-0000-0000-000000000003")


def result_with(*shapes: dict[str, object]) -> AnnotationResult:
    return AnnotationResult.model_validate(
        {
            "schema_version": 1,
            "media_type": "image",
            "classification": {},
            "shapes": list(shapes),
        }
    )


def bbox(shape_id: UUID, coords: list[float], cls: str = "car") -> dict[str, object]:
    return {
        "id": str(shape_id),
        "type": "bbox",
        "class": cls,
        "attributes": {},
        "confidence": None,
        "bbox": coords,
    }


class TestBlobPath:
    def test_matches_the_contract_layout(self) -> None:
        assert (
            blob_path_for(PROJECT_ID, ITEM_ID, 3) == f"annotations/{PROJECT_ID}/{ITEM_ID}/v3.json"
        )

    def test_versions_do_not_collide(self) -> None:
        paths = {blob_path_for(PROJECT_ID, ITEM_ID, n) for n in range(1, 11)}
        assert len(paths) == 10

    def test_outbox_event_name_is_stable(self) -> None:
        # The worker matches on this string; changing it silently breaks publishing.
        assert ANNOTIDE_WRITTEN == "annotation.written"


class TestBlobDocument:
    @pytest.fixture
    def item(self) -> SimpleNamespace:
        return SimpleNamespace(
            id=ITEM_ID,
            path="images/cat.jpg",
            project_id=PROJECT_ID,
            connector_id=uuid4(),
        )

    @pytest.fixture
    def annotation(self) -> SimpleNamespace:
        return SimpleNamespace(
            version=2,
            label_schema_version_id=uuid4(),
            source=SimpleNamespace(value="human"),
            author_user_id=uuid4(),
            author_model_version_id=None,
            status=SimpleNamespace(value="submitted"),
            duration_ms=45_000,
        )

    def test_is_self_contained(self, item: SimpleNamespace, annotation: SimpleNamespace) -> None:
        """DATA-3: a reader needs nothing but this document."""
        document = build_blob_document(
            item=item,
            annotation=annotation,
            result=result_with(bbox(SHAPE_A, [0, 0, 10, 10])),
        )

        for key in (
            "item_id",
            "item_path",
            "project_id",
            "version",
            "label_schema_version_id",
            "annotator_id",
            "status",
            "result",
        ):
            assert key in document, f"blob document is missing {key!r}"

        assert document["item_path"] == "images/cat.jpg"
        assert document["version"] == 2

    def test_is_json_serialisable(self, item: SimpleNamespace, annotation: SimpleNamespace) -> None:
        import json

        document = build_blob_document(
            item=item,
            annotation=annotation,
            result=result_with(bbox(SHAPE_A, [1.5, 2.5, 10, 10])),
        )
        # UUIDs and enums must already be primitives, or the worker's write fails.
        assert json.loads(json.dumps(document))["version"] == 2

    def test_reviewer_is_recorded_when_given(
        self, item: SimpleNamespace, annotation: SimpleNamespace
    ) -> None:
        reviewer = uuid4()
        document = build_blob_document(
            item=item,
            annotation=annotation,
            result=result_with(),
            reviewer_id=reviewer,
        )
        assert document["reviewer_id"] == str(reviewer)

    def test_reviewer_is_null_by_default(
        self, item: SimpleNamespace, annotation: SimpleNamespace
    ) -> None:
        document = build_blob_document(item=item, annotation=annotation, result=result_with())
        assert document["reviewer_id"] is None

    def test_coordinates_survive_unchanged(
        self, item: SimpleNamespace, annotation: SimpleNamespace
    ) -> None:
        # Pixel coordinates must not be rounded or normalised on the way out.
        document = build_blob_document(
            item=item,
            annotation=annotation,
            result=result_with(bbox(SHAPE_A, [120.5, 84.25, 310.0, 240.75])),
        )
        shapes = document["result"]["shapes"]  # type: ignore[index]
        assert shapes[0]["bbox"] == [120.5, 84.25, 310.0, 240.75]


class TestDiffShapes:
    def test_no_changes(self) -> None:
        before = result_with(bbox(SHAPE_A, [0, 0, 10, 10]))
        assert diff_shapes(before, before) == {"added": [], "removed": [], "changed": []}

    def test_added_shape(self) -> None:
        before = result_with(bbox(SHAPE_A, [0, 0, 10, 10]))
        after = result_with(bbox(SHAPE_A, [0, 0, 10, 10]), bbox(SHAPE_B, [20, 20, 30, 30]))

        diff = diff_shapes(before, after)
        assert diff["added"] == [str(SHAPE_B)]
        assert diff["removed"] == []
        assert diff["changed"] == []

    def test_removed_shape(self) -> None:
        before = result_with(bbox(SHAPE_A, [0, 0, 10, 10]), bbox(SHAPE_B, [20, 20, 30, 30]))
        after = result_with(bbox(SHAPE_A, [0, 0, 10, 10]))

        diff = diff_shapes(before, after)
        assert diff["removed"] == [str(SHAPE_B)]
        assert diff["added"] == []

    def test_moved_shape_counts_as_changed_not_replaced(self) -> None:
        """The whole point of stable shape ids (WF-4)."""
        before = result_with(bbox(SHAPE_A, [0, 0, 10, 10]))
        after = result_with(bbox(SHAPE_A, [5, 5, 15, 15]))

        diff = diff_shapes(before, after)
        assert diff["changed"] == [str(SHAPE_A)]
        assert diff["added"] == []
        assert diff["removed"] == []

    def test_reclassified_shape_is_changed(self) -> None:
        before = result_with(bbox(SHAPE_A, [0, 0, 10, 10], cls="car"))
        after = result_with(bbox(SHAPE_A, [0, 0, 10, 10], cls="truck"))
        assert diff_shapes(before, after)["changed"] == [str(SHAPE_A)]

    def test_all_three_at_once(self) -> None:
        before = result_with(bbox(SHAPE_A, [0, 0, 10, 10]), bbox(SHAPE_B, [1, 1, 2, 2]))
        after = result_with(bbox(SHAPE_A, [9, 9, 19, 19]), bbox(SHAPE_C, [3, 3, 4, 4]))

        diff = diff_shapes(before, after)
        assert diff["changed"] == [str(SHAPE_A)]
        assert diff["removed"] == [str(SHAPE_B)]
        assert diff["added"] == [str(SHAPE_C)]

    def test_empty_to_empty(self) -> None:
        empty = result_with()
        assert diff_shapes(empty, empty) == {"added": [], "removed": [], "changed": []}

    def test_first_annotation_is_all_additions(self) -> None:
        diff = diff_shapes(result_with(), result_with(bbox(SHAPE_A, [0, 0, 10, 10])))
        assert diff["added"] == [str(SHAPE_A)]

    def test_output_is_json_friendly(self) -> None:
        diff = diff_shapes(result_with(), result_with(bbox(SHAPE_A, [0, 0, 10, 10])))
        assert all(isinstance(value, str) for value in diff["added"])
