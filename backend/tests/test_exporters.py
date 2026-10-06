"""Tests for app.exporters: registry lookup and per-format round-trip correctness (EXP-7)."""

import json
import uuid

import pytest

from app.exporters import (
    CocoExporter,
    ExportFile,
    ExportItem,
    NativeExporter,
    YoloExporter,
    get_exporter,
)
from app.schemas.annotation import AnnotationResult
from app.schemas.label_schema import ClassDef, LabelSchemaDefinition, ToolType

# ---------------------------------------------------------------------------
# fixtures / builders
# ---------------------------------------------------------------------------


def _definition() -> LabelSchemaDefinition:
    return LabelSchemaDefinition(
        version=1,
        classes=[
            ClassDef(
                name="car",
                display_name="Car",
                color="#e11d48",
                hotkey="1",
                tools=[ToolType.BBOX, ToolType.POLYGON],
            ),
            ClassDef(
                name="road",
                display_name="Road",
                color="#22c55e",
                hotkey="2",
                tools=[ToolType.POLYGON],
            ),
        ],
    )


def _empty_result() -> AnnotationResult:
    return AnnotationResult.model_validate(
        {"schema_version": 1, "media_type": "image", "shapes": []}
    )


def _bbox_item(item_id: uuid.UUID, width: int | None = 100, height: int | None = 100) -> ExportItem:
    result = AnnotationResult.model_validate(
        {
            "schema_version": 1,
            "media_type": "image",
            "shapes": [
                {
                    "id": str(uuid.uuid4()),
                    "type": "bbox",
                    "class": "car",
                    "bbox": [10.0, 20.0, 50.0, 80.0],
                }
            ],
        }
    )
    return ExportItem(id=item_id, path="images/car1.jpg", width=width, height=height, result=result)


def _border_polygon_item(item_id: uuid.UUID) -> ExportItem:
    # A rectangle touching all four edges of a 100x100 image.
    result = AnnotationResult.model_validate(
        {
            "schema_version": 1,
            "media_type": "image",
            "shapes": [
                {
                    "id": str(uuid.uuid4()),
                    "type": "polygon",
                    "class": "road",
                    "points": [[0, 0], [100, 0], [100, 50], [0, 50]],
                }
            ],
        }
    )
    return ExportItem(id=item_id, path="images/road.jpg", width=100, height=100, result=result)


def _rbox_item(item_id: uuid.UUID) -> ExportItem:
    # A 40x20 box centred at (50, 50), rotated 90 degrees: on screen it is 20 wide
    # and 40 tall, so its envelope is (40,30)-(60,70).
    result = AnnotationResult.model_validate(
        {
            "schema_version": 1,
            "media_type": "image",
            "shapes": [
                {
                    "id": str(uuid.uuid4()),
                    "type": "rbox",
                    "class": "car",
                    "center": [50, 50],
                    "size": [40, 20],
                    "angle": 90,
                }
            ],
        }
    )
    return ExportItem(id=item_id, path="images/rbox.jpg", width=100, height=100, result=result)


def _empty_item(item_id: uuid.UUID) -> ExportItem:
    return ExportItem(
        id=item_id, path="images/empty.jpg", width=100, height=100, result=_empty_result()
    )


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_get_exporter_returns_registered_exporters() -> None:
    assert isinstance(get_exporter("native"), NativeExporter)
    assert isinstance(get_exporter("coco"), CocoExporter)
    assert isinstance(get_exporter("yolo"), YoloExporter)


def test_get_exporter_unknown_format_raises_key_error() -> None:
    with pytest.raises(KeyError):
        get_exporter("does-not-exist")


# ---------------------------------------------------------------------------
# native
# ---------------------------------------------------------------------------


def test_native_exporter_round_trip() -> None:
    definition = _definition()
    item_id = uuid.uuid4()
    item = _bbox_item(item_id)

    files = list(NativeExporter().export([item], definition))

    assert len(files) == 1
    assert isinstance(files[0], ExportFile)
    lines = files[0].data.decode("utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["item_id"] == str(item_id)
    assert record["path"] == "images/car1.jpg"
    assert record["width"] == 100
    assert record["height"] == 100
    assert record["shapes"][0]["class"] == "car"
    assert record["shapes"][0]["bbox"] == [10.0, 20.0, 50.0, 80.0]


def test_native_exporter_zero_annotations() -> None:
    definition = _definition()
    item = _empty_item(uuid.uuid4())

    files = list(NativeExporter().export([item], definition))

    record = json.loads(files[0].data.decode("utf-8").strip())
    assert record["shapes"] == []


# ---------------------------------------------------------------------------
# coco — hand-computed expectations
# ---------------------------------------------------------------------------


def test_coco_exporter_bbox_conversion() -> None:
    definition = _definition()
    item = _bbox_item(uuid.uuid4(), width=100, height=100)

    files = list(CocoExporter().export([item], definition))

    assert len(files) == 1
    payload = json.loads(files[0].data.decode("utf-8"))
    assert payload["categories"] == [
        {"id": 1, "name": "car", "supercategory": "none"},
        {"id": 2, "name": "road", "supercategory": "none"},
    ]
    assert payload["images"] == [
        {"id": 1, "file_name": "images/car1.jpg", "width": 100, "height": 100}
    ]
    assert len(payload["annotations"]) == 1
    annotation = payload["annotations"][0]
    # bbox [10, 20, 50, 80] -> xywh [10, 20, 40, 60], area = 40 * 60 = 2400
    assert annotation["image_id"] == 1
    assert annotation["category_id"] == 1  # "car" is first in class order -> id 1
    assert annotation["bbox"] == [10.0, 20.0, 40.0, 60.0]
    assert annotation["area"] == 2400.0
    assert annotation["iscrowd"] == 0
    assert annotation["segmentation"] == []


def test_coco_exporter_polygon_touching_border() -> None:
    definition = _definition()
    item = _border_polygon_item(uuid.uuid4())

    files = list(CocoExporter().export([item], definition))

    payload = json.loads(files[0].data.decode("utf-8"))
    annotation = payload["annotations"][0]
    # rectangle (0,0)-(100,0)-(100,50)-(0,50): bbox [0,0,100,50], area 100*50=5000
    assert annotation["category_id"] == 2  # "road" is second in class order -> id 2
    assert annotation["bbox"] == [0.0, 0.0, 100.0, 50.0]
    assert annotation["area"] == 5000.0
    assert annotation["segmentation"] == [[0.0, 0.0, 100.0, 0.0, 100.0, 50.0, 0.0, 50.0]]


def test_coco_exporter_rbox_as_corners_and_envelope() -> None:
    definition = _definition()
    item = _rbox_item(uuid.uuid4())

    files = list(CocoExporter().export([item], definition))

    payload = json.loads(files[0].data.decode("utf-8"))
    annotation = payload["annotations"][0]
    assert [round(v, 6) for v in annotation["bbox"]] == [40.0, 30.0, 20.0, 40.0]
    assert annotation["area"] == 800.0
    # Corners clockwise from the box's own top-left: (60,30) (60,70) (40,70) (40,30).
    assert [round(v, 6) for v in annotation["segmentation"][0]] == [
        60.0,
        30.0,
        60.0,
        70.0,
        40.0,
        70.0,
        40.0,
        30.0,
    ]


def test_coco_exporter_zero_annotations() -> None:
    definition = _definition()
    item = _empty_item(uuid.uuid4())

    files = list(CocoExporter().export([item], definition))

    payload = json.loads(files[0].data.decode("utf-8"))
    assert payload["images"] == [
        {"id": 1, "file_name": "images/empty.jpg", "width": 100, "height": 100}
    ]
    assert payload["annotations"] == []


# ---------------------------------------------------------------------------
# yolo — hand-computed expectations
# ---------------------------------------------------------------------------


def test_yolo_exporter_bbox_normalisation() -> None:
    definition = _definition()
    item = _bbox_item(uuid.uuid4(), width=100, height=100)

    files = list(YoloExporter().export([item], definition))

    label_files = {f.path: f.data.decode("utf-8") for f in files if f.path.startswith("labels/")}
    # center_x = (10+50)/2/100 = 0.3, center_y = (20+80)/2/100 = 0.5
    # width = 40/100 = 0.4, height = 60/100 = 0.6, class index 0 = "car"
    assert label_files["labels/car1.txt"] == "0 0.300000 0.500000 0.400000 0.600000\n"


def test_yolo_exporter_polygon_touching_border() -> None:
    definition = _definition()
    item = _border_polygon_item(uuid.uuid4())

    files = list(YoloExporter().export([item], definition))

    label_files = {f.path: f.data.decode("utf-8") for f in files if f.path.startswith("labels/")}
    # points normalised by 100x100 and clamped: (0,0) (1,0) (1,0.5) (0,0.5), class index 1 = "road"
    assert label_files["labels/road.txt"] == (
        "1 0.000000 0.000000 1.000000 0.000000 1.000000 0.500000 0.000000 0.500000\n"
    )


def test_yolo_exporter_rbox_as_obb_corners() -> None:
    definition = _definition()
    item = _rbox_item(uuid.uuid4())

    files = list(YoloExporter().export([item], definition))

    label_files = {f.path: f.data.decode("utf-8") for f in files if f.path.startswith("labels/")}
    assert label_files["labels/rbox.txt"] == (
        "0 0.600000 0.300000 0.600000 0.700000 0.400000 0.700000 0.400000 0.300000\n"
    )


def test_yolo_exporter_unknown_dimensions_recorded_not_crashed() -> None:
    definition = _definition()
    item = _bbox_item(uuid.uuid4(), width=None, height=None)

    files = list(YoloExporter().export([item], definition))

    label_files = {f.path: f.data for f in files if f.path.startswith("labels/")}
    assert label_files["labels/car1.txt"].decode("utf-8") == "# skipped: unknown image dimensions\n"


def test_yolo_exporter_zero_annotations() -> None:
    definition = _definition()
    item = _empty_item(uuid.uuid4())

    files = list(YoloExporter().export([item], definition))

    label_files = {f.path: f.data for f in files if f.path.startswith("labels/")}
    assert label_files["labels/empty.txt"] == b""


def test_yolo_exporter_data_yaml_lists_classes_in_index_order() -> None:
    definition = _definition()

    files = list(YoloExporter().export([], definition))

    yaml_files = {f.path: f.data.decode("utf-8") for f in files}
    assert yaml_files["data.yaml"] == "nc: 2\nnames:\n  - car\n  - road\n"


# ---------------------------------------------------------------------------
# text / video items: skipped by image-only formats (CONTRACTS *Annotation result JSON*)
# ---------------------------------------------------------------------------


def _text_item(item_id: uuid.UUID) -> ExportItem:
    result = AnnotationResult.model_validate(
        {
            "schema_version": 1,
            "media_type": "text",
            "shapes": [
                {"id": str(uuid.uuid4()), "type": "span", "class": "car", "start": 0, "end": 3}
            ],
        }
    )
    return ExportItem(id=item_id, path="docs/a.txt", width=None, height=None, result=result)


def _video_item(item_id: uuid.UUID) -> ExportItem:
    result = AnnotationResult.model_validate(
        {
            "schema_version": 1,
            "media_type": "video",
            "shapes": [
                {
                    "id": str(uuid.uuid4()),
                    "type": "bbox",
                    "class": "car",
                    "bbox": [0.0, 0.0, 1.0, 1.0],
                    "frame": 0,
                }
            ],
        }
    )
    return ExportItem(id=item_id, path="clips/a.mp4", width=100, height=100, result=result)


def test_coco_exporter_skips_text_and_video_items_with_warning() -> None:
    definition = _definition()
    items = [_bbox_item(uuid.uuid4()), _text_item(uuid.uuid4()), _video_item(uuid.uuid4())]

    files = list(CocoExporter().export(items, definition))

    payload = json.loads(next(f.data for f in files if f.path == "coco.json").decode("utf-8"))
    assert len(payload["images"]) == 1
    warnings_file = next(f for f in files if f.path == "warnings.json")
    warnings = json.loads(warnings_file.data.decode("utf-8"))
    assert any("docs/a.txt" in w for w in warnings)
    assert any("clips/a.mp4" in w for w in warnings)


def test_coco_exporter_no_warnings_file_when_nothing_skipped() -> None:
    definition = _definition()
    files = list(CocoExporter().export([_bbox_item(uuid.uuid4())], definition))
    assert not any(f.path == "warnings.json" for f in files)


def test_yolo_exporter_skips_text_and_video_items_with_warning() -> None:
    definition = _definition()
    items = [_bbox_item(uuid.uuid4()), _text_item(uuid.uuid4()), _video_item(uuid.uuid4())]

    files = list(YoloExporter().export(items, definition))

    label_paths = {f.path for f in files if f.path.startswith("labels/")}
    assert label_paths == {"labels/car1.txt"}
    warnings_file = next(f for f in files if f.path == "warnings.json")
    warnings = json.loads(warnings_file.data.decode("utf-8"))
    assert any("docs/a.txt" in w for w in warnings)
    assert any("clips/a.mp4" in w for w in warnings)
