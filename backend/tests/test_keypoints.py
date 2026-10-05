"""Keypoint skeletons (TOOL): schema, QA-6 validation, COCO export / import,
agreement (OKS), fusion and the region-split anchor."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.exporters.base import ExportItem
from app.exporters.coco import CocoExporter
from app.importers import ImportFile
from app.importers.base import ImportedItem
from app.importers.coco import CocoImporter
from app.models import Item
from app.schemas.annotation import (
    AnnotationResult,
    KeypointsShape,
    validate_against_schema,
)
from app.schemas.item import MediaType
from app.schemas.label_schema import ClassDef, LabelSchemaDefinition, SkeletonDef, ToolType
from app.services.agreement import keypoint_similarity, match_shapes
from app.services.fusion import fuse
from app.services.importing import convert_item
from app.services.splitting import shape_anchor

SKELETON = SkeletonDef(points=["head", "left_hand", "right_hand"], edges=[(0, 1), (0, 2)])

DEFINITION = LabelSchemaDefinition(
    version=1,
    classes=[
        ClassDef(
            name="person",
            display_name="Person",
            color="#0ea5e9",
            tools=[ToolType.KEYPOINTS],
            skeleton=SKELETON,
        ),
        ClassDef(name="car", display_name="Car", color="#e11d48", tools=[ToolType.BBOX]),
    ],
)


def _keypoints(points: list[tuple[float, float, int]], cls: str = "person") -> KeypointsShape:
    return KeypointsShape(id=uuid.uuid4(), class_=cls, points=points)


def _result(*shapes: Any) -> AnnotationResult:
    return AnnotationResult(schema_version=1, media_type=MediaType.IMAGE, shapes=list(shapes))


PERSON = [(100.0, 50.0, 2), (80.0, 100.0, 1), (0.0, 0.0, 0)]


class TestSchema:
    def test_skeleton_rejects_bad_edges_and_names(self) -> None:
        with pytest.raises(ValidationError, match="out of range"):
            SkeletonDef(points=["a", "b"], edges=[(0, 2)])
        with pytest.raises(ValidationError, match="two different points"):
            SkeletonDef(points=["a", "b"], edges=[(1, 1)])
        with pytest.raises(ValidationError, match="duplicated"):
            SkeletonDef(points=["a", "b"], edges=[(0, 1), (1, 0)])
        with pytest.raises(ValidationError, match="unique"):
            SkeletonDef(points=["a", "a"])
        with pytest.raises(ValidationError, match="at least one point"):
            SkeletonDef(points=[])

    def test_the_keypoints_tool_and_a_skeleton_go_together(self) -> None:
        with pytest.raises(ValidationError, match="requires a skeleton"):
            ClassDef(name="p", display_name="P", color="#000000", tools=[ToolType.KEYPOINTS])
        with pytest.raises(ValidationError, match="requires the keypoints tool"):
            ClassDef(
                name="p",
                display_name="P",
                color="#000000",
                tools=[ToolType.BBOX],
                skeleton=SKELETON,
            )

    def test_shape_needs_a_labelled_point_and_valid_visibility(self) -> None:
        with pytest.raises(ValidationError, match="at least one labelled"):
            _keypoints([(0.0, 0.0, 0)])
        with pytest.raises(ValidationError, match="0, 1 or 2"):
            _keypoints([(1.0, 1.0, 3)])
        shape = _keypoints(PERSON)
        assert shape.labelled() == [(100.0, 50.0), (80.0, 100.0)]
        assert shape.envelope() == (80.0, 50.0, 100.0, 100.0)

    def test_round_trips_through_the_result_json(self) -> None:
        result = _result(_keypoints(PERSON))
        dumped = result.model_dump(mode="json", by_alias=True)
        assert dumped["shapes"][0]["type"] == "keypoints"
        assert AnnotationResult.model_validate(dumped) == result

    def test_validation_checks_the_count_against_the_skeleton(self) -> None:
        assert validate_against_schema(_result(_keypoints(PERSON)), DEFINITION) == []
        [violation] = validate_against_schema(_result(_keypoints([(1.0, 1.0, 2)])), DEFINITION)
        assert "1 keypoints" in violation
        assert "has 3" in violation
        [violation] = validate_against_schema(_result(_keypoints(PERSON, cls="car")), DEFINITION)
        assert "tool 'keypoints' is not allowed" in violation


class TestCoco:
    def _export(self) -> dict[str, Any]:
        item = ExportItem(
            id=uuid.uuid4(),
            path="images/p.jpg",
            width=200,
            height=200,
            result=_result(_keypoints(PERSON)),
        )
        [coco, *_] = list(CocoExporter().export([item], DEFINITION))
        payload: dict[str, Any] = json.loads(coco.data)
        return payload

    def test_export_writes_keypoints_and_a_one_based_skeleton(self) -> None:
        payload = self._export()
        person = payload["categories"][0]
        assert person["keypoints"] == ["head", "left_hand", "right_hand"]
        assert person["skeleton"] == [[1, 2], [1, 3]]
        assert "keypoints" not in payload["categories"][1]
        [annotation] = payload["annotations"]
        assert annotation["keypoints"] == [100.0, 50.0, 2, 80.0, 100.0, 1, 0, 0, 0]
        assert annotation["num_keypoints"] == 2
        assert annotation["bbox"] == [80.0, 50.0, 20.0, 50.0]
        assert annotation["area"] == 1000.0

    def test_export_round_trips_through_the_importer(self) -> None:
        payload = self._export()
        [imported] = list(
            CocoImporter().parse([ImportFile(path="coco.json", data=json.dumps(payload).encode())])
        )
        [shape] = imported.shapes
        assert shape["type"] == "keypoints"
        assert shape["points"] == [[100.0, 50.0, 2], [80.0, 100.0, 1], [0.0, 0.0, 0]]

    def test_import_warns_on_a_count_mismatch_and_keeps_legacy_points(self) -> None:
        payload = {
            "images": [{"id": 1, "file_name": "a.jpg", "width": 10, "height": 10}],
            "categories": [
                {"id": 1, "name": "person", "keypoints": ["a", "b"]},
                {"id": 2, "name": "dot"},
            ],
            "annotations": [
                {"id": 1, "image_id": 1, "category_id": 1, "keypoints": [1, 1, 2]},
                {
                    "id": 2,
                    "image_id": 1,
                    "category_id": 2,
                    "keypoints": [3, 4, 2, 5, 6, 0],
                    "num_keypoints": 1,
                },
            ],
        }
        [imported] = list(
            CocoImporter().parse([ImportFile(path="c.json", data=json.dumps(payload).encode())])
        )
        assert any("1 points, the category names 2" in w for w in imported.warnings)
        assert [s["type"] for s in imported.shapes] == ["point"]


class TestImportMapping:
    def _item(self) -> Item:
        return Item(
            id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            connector_id=uuid.uuid4(),
            path="a.png",
            media_type=MediaType.IMAGE,
            size_bytes=1,
            width=200,
            height=200,
            meta={},
        )

    def test_keypoints_are_dropped_when_the_target_skeleton_does_not_fit(self) -> None:
        imported = ImportedItem(
            path="a",
            shapes=[
                {"type": "keypoints", "class": "person", "points": [list(p) for p in PERSON]},
                {"type": "keypoints", "class": "person", "points": [[1, 1, 2]]},
                {"type": "keypoints", "class": "human", "points": [list(p) for p in PERSON]},
            ],
        )
        converted = convert_item(
            imported, self._item(), DEFINITION, {"human": "car"}, schema_version=1
        )
        assert converted.result is not None
        assert len(converted.result.shapes) == 1
        assert converted.dropped_shapes == 2

    def test_normalised_keypoints_keep_their_visibility(self) -> None:
        imported = ImportedItem(
            path="a",
            coordinates="normalized",
            shapes=[
                {
                    "type": "keypoints",
                    "class": "person",
                    "points": [[0.5, 0.25, 2], [0.1, 0.1, 1], [0, 0, 0]],
                }
            ],
        )
        converted = convert_item(imported, self._item(), DEFINITION, {}, schema_version=1)
        assert converted.result is not None
        [shape] = converted.result.shapes
        assert isinstance(shape, KeypointsShape)
        assert shape.points[0] == (100.0, 50.0, 2)


class TestAgreementAndFusion:
    def test_oks_is_one_for_identical_and_falls_with_distance(self) -> None:
        a = _keypoints(PERSON)
        assert keypoint_similarity(a, _keypoints(PERSON)) == pytest.approx(1.0)
        near = _keypoints([(102.0, 50.0, 2), (80.0, 102.0, 1), (0.0, 0.0, 0)])
        far = _keypoints([(140.0, 50.0, 2), (80.0, 140.0, 1), (0.0, 0.0, 0)])
        assert 0.5 < keypoint_similarity(a, near) < 1.0
        assert keypoint_similarity(a, far) < 0.1
        # A point labelled in only one of the two scores 0.
        partial = _keypoints([(100.0, 50.0, 2), (0.0, 0.0, 0), (0.0, 0.0, 0)])
        assert keypoint_similarity(a, partial) == pytest.approx(0.5)
        assert keypoint_similarity(a, _keypoints([(100.0, 50.0, 2)])) == 0.0

    def test_match_shapes_pairs_skeletons_by_oks(self) -> None:
        near = _keypoints([(101.0, 50.0, 2), (80.0, 101.0, 1), (0.0, 0.0, 0)])
        match = match_shapes([_keypoints(PERSON)], [near])
        assert match.matched == 1

    def test_fusion_averages_each_point_by_majority(self) -> None:
        results = [
            _result(_keypoints([(100.0, 50.0, 2), (80.0, 250.0, 2), (0.0, 0.0, 0)])),
            _result(_keypoints([(102.0, 52.0, 2), (82.0, 252.0, 1), (0.0, 0.0, 0)])),
            _result(_keypoints([(104.0, 54.0, 1), (84.0, 254.0, 1), (300.0, 200.0, 2)])),
        ]
        fused, conflicts = fuse(results)
        assert conflicts == []
        [shape] = fused.shapes
        assert isinstance(shape, KeypointsShape)
        assert shape.points[0] == pytest.approx((102.0, 52.0, 2))
        # visible for one of three: occluded
        assert shape.points[1] == pytest.approx((82.0, 252.0, 1))
        # labelled by one of three: left out
        assert shape.points[2] == (0.0, 0.0, 0)

    def test_region_anchor_is_the_labelled_envelope_centre(self) -> None:
        assert shape_anchor(_keypoints(PERSON)) == (90.0, 75.0)
