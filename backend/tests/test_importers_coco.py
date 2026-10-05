"""Tests for app.importers.coco (EXP-6)."""

import json
import uuid

import pytest

from app.exporters.base import ExportItem
from app.exporters.coco import CocoExporter
from app.importers import IMPORTERS, ImportFile, ImportFormatError, get_importer
from app.importers.coco import CocoImporter
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
                tools=[ToolType.BBOX, ToolType.POLYGON, ToolType.POINT, ToolType.MASK],
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


def _coco_bytes(payload: dict) -> ImportFile:
    return ImportFile(path="coco.json", data=json.dumps(payload).encode("utf-8"))


def _base_payload() -> dict:
    return {
        "images": [{"id": 1, "file_name": "images/car1.jpg", "width": 100, "height": 100}],
        "categories": [
            {"id": 1, "name": "car", "supercategory": "none"},
            {"id": 2, "name": "road", "supercategory": "none"},
        ],
        "annotations": [],
    }


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_registry_lookup_after_import() -> None:
    assert isinstance(get_importer("coco"), CocoImporter)
    assert IMPORTERS["coco"] is get_importer("coco")


# ---------------------------------------------------------------------------
# happy path per shape type
# ---------------------------------------------------------------------------


def test_bbox_happy_path() -> None:
    payload = _base_payload()
    payload["annotations"] = [
        {
            "id": 1,
            "image_id": 1,
            "category_id": 1,
            "bbox": [10.0, 20.0, 40.0, 60.0],
            "attributes": {"occluded": True},
        }
    ]

    items = list(CocoImporter().parse([_coco_bytes(payload)]))

    assert len(items) == 1
    item = items[0]
    assert item.path == "images/car1.jpg"
    assert item.width == 100
    assert item.height == 100
    assert item.coordinates == "pixel"
    assert item.shapes == [
        {
            "type": "bbox",
            "class": "car",
            "bbox": [10.0, 20.0, 50.0, 80.0],
            "attributes": {"occluded": True},
        }
    ]
    assert item.warnings == []


def test_polygon_happy_path() -> None:
    payload = _base_payload()
    payload["annotations"] = [
        {
            "id": 1,
            "image_id": 1,
            "category_id": 2,
            "segmentation": [[0.0, 0.0, 100.0, 0.0, 100.0, 50.0, 0.0, 50.0]],
        }
    ]

    items = list(CocoImporter().parse([_coco_bytes(payload)]))

    assert items[0].shapes == [
        {
            "type": "polygon",
            "class": "road",
            "points": [[0.0, 0.0], [100.0, 0.0], [100.0, 50.0], [0.0, 50.0]],
        }
    ]


def test_mask_happy_path() -> None:
    payload = _base_payload()
    payload["annotations"] = [
        {
            "id": 1,
            "image_id": 1,
            "category_id": 1,
            "segmentation": {"size": [100, 100], "counts": [100, 50, 9850]},
        }
    ]

    items = list(CocoImporter().parse([_coco_bytes(payload)]))

    assert items[0].shapes == [
        {
            "type": "mask",
            "class": "car",
            "rle": {"size": [100, 100], "counts": [100, 50, 9850]},
        }
    ]


def test_keypoint_happy_path() -> None:
    payload = _base_payload()
    payload["annotations"] = [
        {
            "id": 1,
            "image_id": 1,
            "category_id": 1,
            "keypoints": [10.0, 20.0, 2, 30.0, 40.0, 0],
            "num_keypoints": 1,
        }
    ]

    items = list(CocoImporter().parse([_coco_bytes(payload)]))

    # only the visible keypoint (visibility > 0) is emitted
    assert items[0].shapes == [{"type": "point", "class": "car", "point": [10.0, 20.0]}]


def test_image_with_no_annotations_yields_empty_shapes() -> None:
    payload = _base_payload()

    items = list(CocoImporter().parse([_coco_bytes(payload)]))

    assert items[0].shapes == []
    assert items[0].warnings == []


# ---------------------------------------------------------------------------
# malformed input
# ---------------------------------------------------------------------------


def test_invalid_json_raises() -> None:
    file = ImportFile(path="coco.json", data=b"{not json")

    with pytest.raises(ImportFormatError):
        list(CocoImporter().parse([file]))


def test_missing_categories_raises() -> None:
    payload = _base_payload()
    del payload["categories"]

    with pytest.raises(ImportFormatError):
        list(CocoImporter().parse([_coco_bytes(payload)]))


def test_no_coco_json_found_raises() -> None:
    file = ImportFile(path="readme.json", data=json.dumps({"foo": "bar"}).encode("utf-8"))

    with pytest.raises(ImportFormatError):
        list(CocoImporter().parse([file]))


def test_unknown_category_id_raises() -> None:
    payload = _base_payload()
    payload["annotations"] = [
        {"id": 1, "image_id": 1, "category_id": 99, "bbox": [0.0, 0.0, 10.0, 10.0]}
    ]

    with pytest.raises(ImportFormatError):
        list(CocoImporter().parse([_coco_bytes(payload)]))


# ---------------------------------------------------------------------------
# degenerate records -> warnings, not raises
# ---------------------------------------------------------------------------


def test_degenerate_bbox_skipped_with_warning() -> None:
    payload = _base_payload()
    payload["annotations"] = [
        {"id": 1, "image_id": 1, "category_id": 1, "bbox": [0.0, 0.0, 0.0, 10.0]}
    ]

    items = list(CocoImporter().parse([_coco_bytes(payload)]))

    assert items[0].shapes == []
    assert len(items[0].warnings) == 1


def test_degenerate_polygon_skipped_with_warning() -> None:
    payload = _base_payload()
    payload["annotations"] = [
        {"id": 1, "image_id": 1, "category_id": 2, "segmentation": [[0.0, 0.0, 10.0, 10.0]]}
    ]

    items = list(CocoImporter().parse([_coco_bytes(payload)]))

    assert items[0].shapes == []
    assert len(items[0].warnings) == 1


def test_compressed_rle_decoded() -> None:
    payload = _base_payload()
    payload["annotations"] = [
        {
            "id": 1,
            "image_id": 1,
            "category_id": 1,
            # "T3b1jc9" is the pycocotools compressed encoding of [100, 50, 9850].
            "segmentation": {"size": [100, 100], "counts": "T3b1jc9"},
        }
    ]

    items = list(CocoImporter().parse([_coco_bytes(payload)]))

    assert items[0].shapes == [
        {
            "type": "mask",
            "class": "car",
            "rle": {"size": [100, 100], "counts": [100, 50, 9850]},
        }
    ]
    assert items[0].warnings == []


def test_malformed_compressed_rle_skipped_with_warning() -> None:
    payload = _base_payload()
    payload["annotations"] = [
        {
            "id": 1,
            "image_id": 1,
            "category_id": 1,
            # 'P' - 48 = 0x20: a continuation bit with no following byte.
            "segmentation": {"size": [100, 100], "counts": "P"},
        }
    ]

    items = list(CocoImporter().parse([_coco_bytes(payload)]))

    assert items[0].shapes == []
    assert len(items[0].warnings) == 1


def test_compressed_rle_sum_mismatch_skipped_with_warning() -> None:
    payload = _base_payload()
    payload["annotations"] = [
        {
            "id": 1,
            "image_id": 1,
            "category_id": 1,
            # Decodes cleanly to [4, 4] -> sums to 8, not the declared 100x100.
            "segmentation": {"size": [100, 100], "counts": "44"},
        }
    ]

    items = list(CocoImporter().parse([_coco_bytes(payload)]))

    assert items[0].shapes == []
    assert len(items[0].warnings) == 1


# ---------------------------------------------------------------------------
# round-trip through the exporter
# ---------------------------------------------------------------------------


def test_round_trip_through_exporter() -> None:
    definition = _definition()
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
                    "points": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]],
                },
                {
                    "id": str(uuid.uuid4()),
                    "type": "polyline",
                    "class": "road",
                    "points": [[0.0, 0.0], [10.0, 10.0], [20.0, 0.0]],
                },
                {
                    "id": str(uuid.uuid4()),
                    "type": "point",
                    "class": "car",
                    "point": [5.0, 5.0],
                },
                {
                    "id": str(uuid.uuid4()),
                    "type": "mask",
                    "class": "car",
                    "rle": {"size": [10, 10], "counts": [10, 5, 85]},
                },
            ],
        }
    )
    item = ExportItem(id=uuid.uuid4(), path="images/car1.jpg", width=100, height=100, result=result)

    export_files = list(CocoExporter().export([item], definition))
    assert len(export_files) == 1
    import_file = ImportFile(path=export_files[0].path, data=export_files[0].data)

    imported = list(CocoImporter().parse([import_file]))

    assert len(imported) == 1
    shapes = imported[0].shapes
    types = [shape["type"] for shape in shapes]
    # keypoints (from the point shape) are matched before bbox, and the polygon
    # branch also catches the exported polyline: COCO has no dedicated
    # polyline segmentation, so a round-tripped polyline comes back as a
    # polygon.
    assert types.count("bbox") == 1
    assert types.count("polygon") == 2
    assert types.count("point") == 1
    assert types.count("mask") == 1

    bbox_shape = next(s for s in shapes if s["type"] == "bbox")
    assert bbox_shape["class"] == "car"
    assert bbox_shape["bbox"] == [10.0, 20.0, 50.0, 80.0]

    polygon_shapes = [s for s in shapes if s["type"] == "polygon"]
    assert all(s["class"] == "road" for s in polygon_shapes)
    polygon_points = {tuple(tuple(pt) for pt in s["points"]) for s in polygon_shapes}
    assert ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0)) in polygon_points
    assert ((0.0, 0.0), (10.0, 10.0), (20.0, 0.0)) in polygon_points

    point_shape = next(s for s in shapes if s["type"] == "point")
    assert point_shape["class"] == "car"
    assert point_shape["point"] == [5.0, 5.0]

    mask_shape = next(s for s in shapes if s["type"] == "mask")
    assert mask_shape["class"] == "car"
    assert mask_shape["rle"] == {"size": [10, 10], "counts": [10, 5, 85]}
