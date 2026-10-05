"""Tests for app.importers.yolo (EXP-6)."""

import uuid

import pytest

from app.exporters.base import ExportItem
from app.exporters.yolo import YoloExporter
from app.importers import IMPORTERS, ImportFile, ImportFormatError, get_importer
from app.importers.yolo import YoloImporter
from app.schemas.annotation import AnnotationResult
from app.schemas.label_schema import ClassDef, LabelSchemaDefinition, ToolType

TOLERANCE = 1e-3


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
                tools=[ToolType.POLYGON, ToolType.POLYLINE],
            ),
        ],
    )


def _yaml_block(names: str = "  - car\n  - road\n") -> ImportFile:
    return ImportFile(path="data.yaml", data=f"nc: 2\nnames:\n{names}".encode())


def _label(path: str, content: str) -> ImportFile:
    return ImportFile(path=path, data=content.encode("utf-8"))


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_registry_lookup_after_import() -> None:
    assert isinstance(get_importer("yolo"), YoloImporter)
    assert IMPORTERS["yolo"] is get_importer("yolo")


# ---------------------------------------------------------------------------
# happy path per shape type / class-name formats
# ---------------------------------------------------------------------------


def test_bbox_happy_path() -> None:
    files = [_yaml_block(), _label("labels/car1.txt", "0 0.3 0.5 0.4 0.6\n")]

    items = list(YoloImporter().parse(files))

    assert len(items) == 1
    item = items[0]
    assert item.path == "car1"
    assert item.width is None
    assert item.height is None
    assert item.coordinates == "normalized"
    assert item.warnings == []
    assert len(item.shapes) == 1
    shape = item.shapes[0]
    assert shape["type"] == "bbox"
    assert shape["class"] == "car"
    assert shape["bbox"] == pytest.approx([0.1, 0.2, 0.5, 0.8], abs=TOLERANCE)


def test_bbox_with_confidence() -> None:
    files = [_yaml_block(), _label("labels/car1.txt", "0 0.3 0.5 0.4 0.6 0.9\n")]

    items = list(YoloImporter().parse(files))

    shape = items[0].shapes[0]
    assert shape["confidence"] == pytest.approx(0.9)


def test_polygon_happy_path() -> None:
    files = [
        _yaml_block(),
        _label("labels/road.txt", "1 0.0 0.0 1.0 0.0 1.0 0.5 0.0 0.5\n"),
    ]

    items = list(YoloImporter().parse(files))

    shape = items[0].shapes[0]
    assert shape["type"] == "polygon"
    assert shape["class"] == "road"
    assert shape["points"] == [[0.0, 0.0], [1.0, 0.0], [1.0, 0.5], [0.0, 0.5]]


def test_inline_names_list() -> None:
    yaml_file = ImportFile(path="data.yaml", data=b"names: [car, road]\nnc: 2\n")
    files = [yaml_file, _label("labels/car1.txt", "1 0.3 0.5 0.4 0.6\n")]

    items = list(YoloImporter().parse(files))

    assert items[0].shapes[0]["class"] == "road"


def test_index_mapping_names() -> None:
    yaml_file = ImportFile(path="data.yaml", data=b"names:\n  0: car\n  1: road\n")
    files = [yaml_file, _label("labels/road.txt", "1 0.3 0.5 0.4 0.6\n")]

    items = list(YoloImporter().parse(files))

    assert items[0].shapes[0]["class"] == "road"


def test_comment_and_blank_lines_skipped() -> None:
    files = [
        _yaml_block(),
        _label("labels/car1.txt", "# skipped: unknown image dimensions\n\n0 0.3 0.5 0.4 0.6\n"),
    ]

    items = list(YoloImporter().parse(files))

    assert len(items[0].shapes) == 1


def test_no_names_file_falls_back_to_numeric_index_with_warning() -> None:
    files = [_label("labels/car1.txt", "0 0.3 0.5 0.4 0.6\n")]

    items = list(YoloImporter().parse(files))

    assert items[0].shapes[0]["class"] == "0"
    assert len(items[0].warnings) == 1


def test_label_file_stem_yielded_even_without_match() -> None:
    files = [_yaml_block(), _label("labels/does_not_exist.txt", "0 0.3 0.5 0.4 0.6\n")]

    items = list(YoloImporter().parse(files))

    assert items[0].path == "does_not_exist"


# ---------------------------------------------------------------------------
# malformed input
# ---------------------------------------------------------------------------


def test_no_label_files_raises() -> None:
    with pytest.raises(ImportFormatError):
        list(YoloImporter().parse([_yaml_block()]))


# ---------------------------------------------------------------------------
# degenerate / malformed lines -> warnings, not raises
# ---------------------------------------------------------------------------


def test_malformed_line_skipped_with_warning() -> None:
    files = [_yaml_block(), _label("labels/car1.txt", "0 not a number 0.5 0.4\n")]

    items = list(YoloImporter().parse(files))

    assert items[0].shapes == []
    assert len(items[0].warnings) == 1


def test_unknown_class_index_skipped_with_warning() -> None:
    files = [_yaml_block(), _label("labels/car1.txt", "5 0.3 0.5 0.4 0.6\n")]

    items = list(YoloImporter().parse(files))

    assert items[0].shapes == []
    assert len(items[0].warnings) == 1


def test_degenerate_bbox_skipped_with_warning() -> None:
    files = [_yaml_block(), _label("labels/car1.txt", "0 0.3 0.5 0.0 0.6\n")]

    items = list(YoloImporter().parse(files))

    assert items[0].shapes == []
    assert len(items[0].warnings) == 1


def test_odd_coordinate_count_skipped_with_warning() -> None:
    # 5 coords after class index that aren't (cx, cy, w, h, conf)-shaped is fine (bbox+conf),
    # but an odd count >= 7 is neither a bbox nor a valid polygon.
    files = [_yaml_block(), _label("labels/car1.txt", "0 0.1 0.2 0.3 0.4 0.5 0.6 0.7\n")]

    items = list(YoloImporter().parse(files))

    assert items[0].shapes == []
    assert len(items[0].warnings) == 1


# ---------------------------------------------------------------------------
# round-trip through the exporter
# ---------------------------------------------------------------------------


def test_round_trip_through_exporter() -> None:
    definition = _definition()
    width, height = 100.0, 100.0
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
                },
                {
                    "id": str(uuid.uuid4()),
                    "type": "polygon",
                    "class": "road",
                    "points": [[0.0, 0.0], [100.0, 0.0], [100.0, 50.0], [0.0, 50.0]],
                },
            ],
        }
    )
    item = ExportItem(
        id=uuid.uuid4(), path="images/car1.jpg", width=int(width), height=int(height), result=result
    )

    export_files = list(YoloExporter().export([item], definition))
    import_files = [ImportFile(path=f.path, data=f.data) for f in export_files]

    items = list(YoloImporter().parse(import_files))

    label_item = next(i for i in items if i.path == "car1")
    assert len(label_item.shapes) == 2

    bbox_shape = next(s for s in label_item.shapes if s["type"] == "bbox")
    assert bbox_shape["class"] == "car"
    # de-normalise using the known image dimensions and compare to the original pixel bbox
    denormalised = [
        bbox_shape["bbox"][0] * width,
        bbox_shape["bbox"][1] * height,
        bbox_shape["bbox"][2] * width,
        bbox_shape["bbox"][3] * height,
    ]
    assert denormalised == pytest.approx(
        [10.0, 20.0, 50.0, 80.0], abs=TOLERANCE * max(width, height)
    )

    polygon_shape = next(s for s in label_item.shapes if s["type"] == "polygon")
    assert polygon_shape["class"] == "road"
    denormalised_points = [c for x, y in polygon_shape["points"] for c in (x * width, y * height)]
    assert denormalised_points == pytest.approx(
        [0.0, 0.0, 100.0, 0.0, 100.0, 50.0, 0.0, 50.0], abs=TOLERANCE * max(width, height)
    )
