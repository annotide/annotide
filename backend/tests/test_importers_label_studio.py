"""Tests for app.importers.label_studio: Label Studio task export parsing (EXP-6)."""

from __future__ import annotations

import json

import pytest

from app.importers import IMPORTERS, ImportFile, ImportFormatError, get_importer
from app.importers.label_studio import LabelStudioImporter

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _task(**overrides: object) -> dict[str, object]:
    task: dict[str, object] = {
        "id": 1,
        "data": {"image": "imgs/a.jpg"},
        "annotations": [],
    }
    task.update(overrides)
    return task


def _file(tasks: object, path: str = "export.json") -> ImportFile:
    return ImportFile(path=path, data=json.dumps(tasks).encode())


def _annotation(
    results: list[dict[str, object]], *, was_cancelled: bool = False
) -> dict[str, object]:
    return {"result": results, "was_cancelled": was_cancelled}


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def test_label_studio_importer_registered() -> None:
    assert isinstance(get_importer("label_studio"), LabelStudioImporter)
    assert IMPORTERS["label_studio"] is get_importer("label_studio")


# ---------------------------------------------------------------------------
# shape conversion — pixel coordinates (original_width/height present)
# ---------------------------------------------------------------------------


def test_rectangle_converted_to_pixel_bbox() -> None:
    result = {
        "type": "rectanglelabels",
        "from_name": "label",
        "to_name": "image",
        "original_width": 200,
        "original_height": 100,
        "value": {"x": 10.0, "y": 20.0, "width": 30.0, "height": 40.0, "rectanglelabels": ["car"]},
    }
    task = _task(annotations=[_annotation([result])])
    items = list(LabelStudioImporter().parse([_file([task])]))

    assert len(items) == 1
    item = items[0]
    assert item.path == "imgs/a.jpg"
    assert item.width == 200
    assert item.height == 100
    assert item.coordinates == "pixel"
    # x_min = 10% of 200 = 20, y_min = 20% of 100 = 20
    # x_max = x_min + 30% of 200 = 20 + 60 = 80, y_max = 20 + 40% of 100 = 20 + 40 = 60
    assert item.shapes == [{"type": "bbox", "class": "car", "bbox": [20.0, 20.0, 80.0, 60.0]}]
    assert item.warnings == []


def test_polygon_converted_to_pixel_points() -> None:
    result = {
        "type": "polygonlabels",
        "original_width": 100,
        "original_height": 200,
        "value": {
            "points": [[0.0, 0.0], [50.0, 0.0], [50.0, 50.0]],
            "polygonlabels": ["road"],
        },
    }
    task = _task(annotations=[_annotation([result])])
    items = list(LabelStudioImporter().parse([_file([task])]))

    item = items[0]
    assert item.coordinates == "pixel"
    assert item.shapes == [
        {
            "type": "polygon",
            "class": "road",
            "points": [[0.0, 0.0], [50.0, 0.0], [50.0, 100.0]],
        }
    ]


def test_keypoint_converted_to_pixel_point() -> None:
    result = {
        "type": "keypointlabels",
        "original_width": 100,
        "original_height": 100,
        "value": {"x": 25.0, "y": 75.0, "keypointlabels": ["eye"]},
    }
    task = _task(annotations=[_annotation([result])])
    items = list(LabelStudioImporter().parse([_file([task])]))

    item = items[0]
    assert item.coordinates == "pixel"
    assert item.shapes == [{"type": "point", "class": "eye", "point": [25.0, 75.0]}]


# ---------------------------------------------------------------------------
# normalised fallback — no original_width/original_height anywhere
# ---------------------------------------------------------------------------


def test_normalized_fallback_without_dimensions() -> None:
    result = {
        "type": "rectanglelabels",
        "value": {"x": 10.0, "y": 20.0, "width": 30.0, "height": 40.0, "rectanglelabels": ["car"]},
    }
    task = _task(annotations=[_annotation([result])])
    items = list(LabelStudioImporter().parse([_file([task])]))

    item = items[0]
    assert item.width is None
    assert item.height is None
    assert item.coordinates == "normalized"
    assert len(item.shapes) == 1
    shape = item.shapes[0]
    assert shape["type"] == "bbox"
    assert shape["class"] == "car"
    assert shape["bbox"] == pytest.approx([0.1, 0.2, 0.4, 0.6])


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def test_choices_single_value_classification() -> None:
    result = {"type": "choices", "from_name": "quality", "value": {"choices": ["good"]}}
    task = _task(annotations=[_annotation([result])])
    items = list(LabelStudioImporter().parse([_file([task])]))

    assert items[0].classification == {"quality": "good"}


def test_choices_multi_value_classification() -> None:
    result = {"type": "choices", "from_name": "tags", "value": {"choices": ["a", "b"]}}
    task = _task(annotations=[_annotation([result])])
    items = list(LabelStudioImporter().parse([_file([task])]))

    assert items[0].classification == {"tags": ["a", "b"]}


def test_textarea_classification_joined() -> None:
    result = {"type": "textarea", "from_name": "notes", "value": {"text": ["line1", "line2"]}}
    task = _task(annotations=[_annotation([result])])
    items = list(LabelStudioImporter().parse([_file([task])]))

    assert items[0].classification == {"notes": "line1\nline2"}


# ---------------------------------------------------------------------------
# annotation / prediction selection
# ---------------------------------------------------------------------------


def test_predictions_used_when_no_annotations_with_warning() -> None:
    prediction_result = {
        "type": "rectanglelabels",
        "original_width": 100,
        "original_height": 100,
        "value": {"x": 0.0, "y": 0.0, "width": 10.0, "height": 10.0, "rectanglelabels": ["car"]},
    }
    task = _task(annotations=[], predictions=[{"result": [prediction_result]}])
    items = list(LabelStudioImporter().parse([_file([task])]))

    item = items[0]
    assert item.shapes[0]["class"] == "car"
    assert any("prediction" in warning for warning in item.warnings)


def test_cancelled_annotations_are_skipped() -> None:
    cancelled_result = {
        "type": "rectanglelabels",
        "original_width": 100,
        "original_height": 100,
        "value": {"x": 0.0, "y": 0.0, "width": 5.0, "height": 5.0, "rectanglelabels": ["wrong"]},
    }
    good_result = {
        "type": "rectanglelabels",
        "original_width": 100,
        "original_height": 100,
        "value": {"x": 0.0, "y": 0.0, "width": 10.0, "height": 10.0, "rectanglelabels": ["car"]},
    }
    task = _task(
        annotations=[
            _annotation([cancelled_result], was_cancelled=True),
            _annotation([good_result], was_cancelled=False),
        ]
    )
    items = list(LabelStudioImporter().parse([_file([task])]))

    item = items[0]
    assert item.shapes == [{"type": "bbox", "class": "car", "bbox": [0.0, 0.0, 10.0, 10.0]}]
    assert any("annotations" in warning for warning in item.warnings)


def test_task_with_no_results_yields_empty_item() -> None:
    task = _task(annotations=[_annotation([])])
    items = list(LabelStudioImporter().parse([_file([task])]))

    assert len(items) == 1
    assert items[0].shapes == []
    assert items[0].classification == {}


# ---------------------------------------------------------------------------
# warnings / skips
# ---------------------------------------------------------------------------


def test_rotated_rectangle_skipped_with_warning() -> None:
    result = {
        "type": "rectanglelabels",
        "original_width": 100,
        "original_height": 100,
        "value": {
            "x": 0.0,
            "y": 0.0,
            "width": 10.0,
            "height": 10.0,
            "rotation": 45.0,
            "rectanglelabels": ["car"],
        },
    }
    task = _task(annotations=[_annotation([result])])
    items = list(LabelStudioImporter().parse([_file([task])]))

    item = items[0]
    assert item.shapes == []
    assert any("rotat" in warning for warning in item.warnings)


def _encode_brush_rle(groups_of_8: list[int]) -> list[int]:
    """Encode a Label Studio brush RLE using only constant-span (flag=1) groups.

    Mirrors the bit layout `decode_label_studio_brush` reads (MSB-first
    fields): a 32-bit length, a 5-bit word size, four 4-bit (unused) RLE
    sizes, then one flag bit + 8-bit value per 8-byte span of the flat RGBA
    buffer. `groups_of_8` gives one byte value per span.
    """
    bits: list[str] = []

    def write(value: int, size: int) -> None:
        bits.append(format(value, f"0{size}b"))

    write(len(groups_of_8) * 8, 32)
    write(7, 5)  # word_size - 1 = 7 -> word_size = 8
    for _ in range(4):
        write(0, 4)
    for value in groups_of_8:
        write(1, 1)
        write(value, 8)

    bitstring = "".join(bits)
    bitstring += "0" * ((-len(bitstring)) % 8)
    return [int(bitstring[i : i + 8], 2) for i in range(0, len(bitstring), 8)]


def test_brushlabels_decoded_to_mask() -> None:
    # width=4, height=2; alpha pattern (row-major) is [0,0,1,1, 0,0,1,1].
    result = {
        "type": "brushlabels",
        "original_width": 4,
        "original_height": 2,
        "value": {
            "brushlabels": ["car"],
            "format": "rle",
            "rle": _encode_brush_rle([0, 255, 0, 255]),
        },
    }
    task = _task(annotations=[_annotation([result])])
    items = list(LabelStudioImporter().parse([_file([task])]))

    item = items[0]
    assert item.shapes == [
        {"type": "mask", "class": "car", "rle": {"size": [2, 4], "counts": [4, 4]}}
    ]
    assert item.warnings == []


def test_brushlabels_without_dimensions_skipped_with_warning() -> None:
    result = {"type": "brushlabels", "value": {"brushlabels": ["car"], "rle": [1, 2, 3]}}
    task = _task(annotations=[_annotation([result])])
    items = list(LabelStudioImporter().parse([_file([task])]))

    item = items[0]
    assert item.shapes == []
    assert any("brush" in warning for warning in item.warnings)


def test_brushlabels_malformed_rle_skipped_with_warning() -> None:
    result = {
        "type": "brushlabels",
        "original_width": 4,
        "original_height": 2,
        "value": {"brushlabels": ["car"], "format": "rle", "rle": [1, 2, 3]},
    }
    task = _task(annotations=[_annotation([result])])
    items = list(LabelStudioImporter().parse([_file([task])]))

    item = items[0]
    assert item.shapes == []
    assert any("brush" in warning for warning in item.warnings)


def test_brushlabels_unsupported_format_skipped_with_warning() -> None:
    result = {
        "type": "brushlabels",
        "original_width": 4,
        "original_height": 2,
        "value": {"brushlabels": ["car"], "format": "png", "rle": [1, 2, 3]},
    }
    task = _task(annotations=[_annotation([result])])
    items = list(LabelStudioImporter().parse([_file([task])]))

    item = items[0]
    assert item.shapes == []
    assert any("brush" in warning for warning in item.warnings)


def test_text_span_labels_converted_to_span_shape() -> None:
    result = {
        "id": "r1",
        "type": "labels",
        "value": {"start": 0, "end": 4, "text": "John", "labels": ["PERSON"]},
    }
    task = _task(data={"text": "John works at Acme"}, annotations=[_annotation([result])])
    items = list(LabelStudioImporter().parse([_file([task])]))

    item = items[0]
    assert item.coordinates == "pixel"
    assert len(item.shapes) == 1
    span = item.shapes[0]
    assert span["type"] == "span"
    assert span["class"] == "PERSON"
    assert span["start"] == 0
    assert span["end"] == 4
    assert span["text"] == "John"


def test_relation_between_two_spans_converted() -> None:
    per = {
        "id": "r1",
        "type": "labels",
        "value": {"start": 0, "end": 4, "labels": ["PERSON"]},
    }
    org = {
        "id": "r2",
        "type": "labels",
        "value": {"start": 14, "end": 18, "labels": ["ORG"]},
    }
    relation = {"type": "relation", "from_id": "r1", "to_id": "r2", "labels": ["works_for"]}
    task = _task(
        data={"text": "John works at Acme"}, annotations=[_annotation([per, org, relation])]
    )
    items = list(LabelStudioImporter().parse([_file([task])]))

    item = items[0]
    by_type = {shape["type"]: shape for shape in item.shapes}
    assert "relation" in by_type
    relation_shape = by_type["relation"]
    assert relation_shape["class"] == "works_for"
    span_ids = {shape["id"] for shape in item.shapes if shape["type"] == "span"}
    assert relation_shape["from"] in span_ids
    assert relation_shape["to"] in span_ids
    assert relation_shape["from"] != relation_shape["to"]


def test_relation_with_unmatched_region_skipped_with_warning() -> None:
    relation = {"type": "relation", "from_id": "missing1", "to_id": "missing2", "labels": ["x"]}
    task = _task(annotations=[_annotation([relation])])
    items = list(LabelStudioImporter().parse([_file([task])]))

    item = items[0]
    assert not any(shape["type"] == "relation" for shape in item.shapes)
    assert any("unmatched region id" in warning for warning in item.warnings)


def test_unknown_result_type_with_labels_list_warns() -> None:
    result = {"type": "somethingnew", "value": {"somethingnewlabels": ["x"], "labels": ["x"]}}
    task = _task(annotations=[_annotation([result])])
    items = list(LabelStudioImporter().parse([_file([task])]))

    item = items[0]
    assert item.shapes == []
    assert any("unsupported" in warning for warning in item.warnings)


# ---------------------------------------------------------------------------
# path normalisation
# ---------------------------------------------------------------------------


def test_path_https_url_strips_scheme_host_and_query() -> None:
    task = _task(data={"image": "https://host/bucket/imgs/a.jpg?x=1"}, annotations=[])
    items = list(LabelStudioImporter().parse([_file([task])]))

    assert items[0].path == "bucket/imgs/a.jpg"


def test_path_s3_scheme_drops_bucket_netloc() -> None:
    task = _task(data={"image": "s3://bucket/imgs/a.jpg"}, annotations=[])
    items = list(LabelStudioImporter().parse([_file([task])]))

    assert items[0].path == "imgs/a.jpg"


def test_path_local_files_query_param() -> None:
    task = _task(data={"image": "/data/local-files/?d=imgs/a.jpg"}, annotations=[])
    items = list(LabelStudioImporter().parse([_file([task])]))

    assert items[0].path == "imgs/a.jpg"


def test_path_plain_relative_path_unchanged() -> None:
    task = _task(data={"image": "imgs/a.jpg"}, annotations=[])
    items = list(LabelStudioImporter().parse([_file([task])]))

    assert items[0].path == "imgs/a.jpg"


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------


def test_malformed_json_raises() -> None:
    files = [ImportFile(path="bad.json", data=b"{not json")]

    with pytest.raises(ImportFormatError):
        list(LabelStudioImporter().parse(files))


def test_non_task_json_raises() -> None:
    files = [_file({"images": [], "annotations": []})]  # e.g. a COCO document

    with pytest.raises(ImportFormatError):
        list(LabelStudioImporter().parse(files))


def test_list_with_non_task_entries_raises() -> None:
    files = [_file([{"no_data_field": True}])]

    with pytest.raises(ImportFormatError):
        list(LabelStudioImporter().parse(files))


def test_no_json_files_raises() -> None:
    files = [ImportFile(path="readme.txt", data=b"hello")]

    with pytest.raises(ImportFormatError):
        list(LabelStudioImporter().parse(files))


# ---------------------------------------------------------------------------
# single-task (non-list) file
# ---------------------------------------------------------------------------


def test_single_task_object_not_wrapped_in_list() -> None:
    task = _task(data={"image": "imgs/a.jpg"}, annotations=[])
    files = [_file(task)]

    items = list(LabelStudioImporter().parse(files))

    assert len(items) == 1
    assert items[0].path == "imgs/a.jpg"
