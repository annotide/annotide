"""Tests for the `yolo_pose` exporter (TOOL keypoint skeletons, EXP-7)."""

import uuid
from typing import Any

from app.exporters import ExportItem, YoloPoseExporter, get_exporter
from app.exporters.yolo_pose import flip_index
from app.schemas.annotation import AnnotationResult
from app.schemas.label_schema import ClassDef, LabelSchemaDefinition, SkeletonDef, ToolType


def _definition(*extra: ClassDef) -> LabelSchemaDefinition:
    return LabelSchemaDefinition(
        version=1,
        classes=[
            ClassDef(name="car", display_name="Car", color="#e11d48", tools=[ToolType.BBOX]),
            ClassDef(
                name="person",
                display_name="Person",
                color="#a855f7",
                tools=[ToolType.KEYPOINTS],
                skeleton=SkeletonDef(
                    points=["nose", "left_eye", "right_eye"], edges=[(0, 1), (0, 2)]
                ),
            ),
            *extra,
        ],
    )


_HAND = ClassDef(
    name="hand",
    display_name="Hand",
    color="#0ea5e9",
    tools=[ToolType.KEYPOINTS],
    skeleton=SkeletonDef(points=["wrist", "tip"], edges=[(0, 1)]),
)


def _item(shapes: list[dict[str, Any]], *, width: int | None = 100) -> ExportItem:
    result = AnnotationResult.model_validate(
        {"schema_version": 1, "media_type": "image", "shapes": shapes}
    )
    return ExportItem(id=uuid.uuid4(), path="img/a.jpg", width=width, height=100, result=result)


def _keypoints(class_: str, points: list[list[float]]) -> dict[str, Any]:
    return {"id": str(uuid.uuid4()), "type": "keypoints", "class": class_, "points": points}


def _files(items: list[ExportItem], definition: LabelSchemaDefinition) -> dict[str, str]:
    return {f.path: f.data.decode() for f in YoloPoseExporter().export(items, definition)}


def test_registered() -> None:
    assert isinstance(get_exporter("yolo_pose"), YoloPoseExporter)


def test_line_is_envelope_then_points_with_unlabelled_zeroed() -> None:
    item = _item(
        [
            _keypoints("person", [[20, 40, 2], [60, 80, 1], [99, 99, 0]]),
            {"id": str(uuid.uuid4()), "type": "bbox", "class": "car", "bbox": [0, 0, 5, 5]},
        ]
    )
    files = _files([item], _definition())

    # envelope of labelled points (20,40)-(60,80): center 0.4,0.6 size 0.4,0.4
    assert files["labels/a.txt"] == (
        "0 0.400000 0.600000 0.400000 0.400000 0.200000 0.400000 2 0.600000 0.800000 1 0 0 0\n"
    )


def test_shorter_skeletons_are_padded_to_the_longest() -> None:
    item = _item([_keypoints("hand", [[10, 10, 2], [30, 50, 2]])])
    files = _files([item], _definition(_HAND))

    line = files["labels/a.txt"].split()
    assert line[0] == "1"  # hand: second keypoints class, bbox-only `car` not indexed
    assert len(line) == 1 + 4 + 3 * 3
    assert line[-3:] == ["0", "0", "0"]


def test_data_yaml_has_kpt_shape_names_and_flip_idx() -> None:
    files = _files([], _definition())

    assert files["data.yaml"] == (
        "kpt_shape: [3, 3]\nflip_idx: [0, 2, 1]\nnc: 1\nnames:\n  - person\n"
    )
    assert "warnings.json" not in files


def test_flip_idx_omitted_when_skeletons_differ() -> None:
    files = _files([], _definition(_HAND))

    assert "flip_idx" not in files["data.yaml"]
    assert "kpt_shape: [3, 3]" in files["data.yaml"]
    assert "  - person\n  - hand\n" in files["data.yaml"]


def test_flip_index_keeps_case_and_pads() -> None:
    assert flip_index(["Left_Hip", "RIGHT_HIP", "neck"], 4) == [1, 0, 2, 3]


def test_schema_without_keypoints_class_warns() -> None:
    files = _files([_item([])], LabelSchemaDefinition(version=1, classes=[]))

    assert files["labels/a.txt"] == ""
    assert "no keypoints class" in files["warnings.json"]
    assert "kpt_shape: [0, 3]" in files["data.yaml"]


def test_unknown_dimensions_recorded() -> None:
    item = _item([_keypoints("person", [[1, 1, 2], [2, 2, 2], [3, 3, 2]])], width=None)
    files = _files([item], _definition())

    assert files["labels/a.txt"].startswith("# skipped")


def test_text_items_are_skipped_with_warning() -> None:
    text = ExportItem(
        id=uuid.uuid4(),
        path="doc.txt",
        width=None,
        height=None,
        result=AnnotationResult.model_validate(
            {"schema_version": 1, "media_type": "text", "shapes": []}
        ),
    )
    files = _files([text], _definition())

    assert "doc.txt" in files["warnings.json"]
    assert "labels/doc.txt" not in files
