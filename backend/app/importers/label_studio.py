"""Label Studio JSON export importer (EXP-6).

Parses one or more Label Studio task exports (`{"data": ..., "annotations":
[...], "predictions": [...]}`, either a single task object or a list of
tasks) into :class:`ImportedItem` records. Label Studio expresses region
coordinates as percentages of the original image; this importer converts
them to pixels when `original_width`/`original_height` are present on a
result, or leaves them as `[0, 1]` fractions otherwise.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from app.importers.base import (
    Coordinates,
    ImportedItem,
    ImportFile,
    ImportFormatError,
    register_importer,
)
from app.importers.rle import decode_label_studio_brush

_PATH_KEYS = ("image", "img", "url", "video", "audio", "text", "pdf")
_LABEL_LIST_KEYS = ("rectanglelabels", "polygonlabels", "keypointlabels", "brushlabels", "labels")


def _normalize_path(value: str) -> str:
    """Turn a Label Studio `data` value into a relative-ish path.

    Strips URL scheme, host and query string; resolves Label Studio's
    `local-files` serving convention (`?d=<path>`) to the underlying path.
    """
    parsed = urlsplit(value)

    if "local-files" in parsed.path:
        d_values = parse_qs(parsed.query).get("d")
        if d_values:
            return d_values[0].lstrip("/")

    if parsed.scheme:
        return parsed.path.lstrip("/")

    return value


def _extract_path(data: dict[str, Any]) -> str | None:
    for key in _PATH_KEYS:
        value = data.get(key)
        if isinstance(value, str):
            return _normalize_path(value)
    for value in data.values():
        if isinstance(value, str):
            return _normalize_path(value)
    return None


def _load_tasks(file: ImportFile) -> list[dict[str, Any]]:
    try:
        payload = json.loads(file.data)
    except json.JSONDecodeError as exc:
        raise ImportFormatError(f"{file.path}: not valid JSON") from exc

    candidates: list[Any]
    if isinstance(payload, dict):
        candidates = [payload]
    elif isinstance(payload, list):
        candidates = payload
    else:
        raise ImportFormatError(f"{file.path}: expected a Label Studio task or list of tasks")

    tasks: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or "data" not in candidate:
            raise ImportFormatError(f"{file.path}: expected Label Studio tasks with a 'data' field")
        tasks.append(candidate)
    return tasks


def _select_annotation(task: dict[str, Any], warnings: list[str]) -> dict[str, Any] | None:
    """Pick the annotation to import: first non-cancelled, else the first prediction."""
    annotations = task.get("annotations")
    if isinstance(annotations, list) and annotations:
        non_cancelled = [
            item for item in annotations if isinstance(item, dict) and not item.get("was_cancelled")
        ]
        if non_cancelled:
            if len(annotations) > 1:
                warnings.append(
                    f"task has {len(annotations)} annotations; using the first non-cancelled one"
                )
            return non_cancelled[0]

    predictions = task.get("predictions")
    if isinstance(predictions, list) and predictions and isinstance(predictions[0], dict):
        warnings.append("no usable annotations; using the first prediction instead")
        return predictions[0]

    return None


def _task_dimensions(results: list[dict[str, Any]]) -> tuple[int | None, int | None]:
    for result in results:
        width = result.get("original_width")
        height = result.get("original_height")
        if width is not None and height is not None:
            return int(width), int(height)
    return None, None


def _scale(percent: float, size: int | None) -> float:
    fraction = percent / 100.0
    return fraction * size if size is not None else fraction


def _rectangle_shape(
    value: dict[str, Any], width: int | None, height: int | None, warnings: list[str]
) -> dict[str, Any] | None:
    labels = value.get("rectanglelabels")
    if not isinstance(labels, list) or not labels:
        warnings.append("skipped rectangle: missing 'rectanglelabels'")
        return None
    if len(labels) > 1:
        warnings.append(f"rectangle has multiple labels {labels!r}; using the first")

    rotation = value.get("rotation") or 0
    if rotation:
        warnings.append(f"skipped rotated rectangle (rotation={rotation}): no bbox mapping")
        return None

    try:
        x = float(value["x"])
        y = float(value["y"])
        box_width = float(value["width"])
        box_height = float(value["height"])
    except (KeyError, TypeError, ValueError):
        warnings.append("skipped rectangle: missing or non-numeric x/y/width/height")
        return None

    if box_width <= 0 or box_height <= 0:
        warnings.append("skipped degenerate rectangle: zero width or height")
        return None

    x_min = _scale(x, width)
    y_min = _scale(y, height)
    x_max = x_min + _scale(box_width, width)
    y_max = y_min + _scale(box_height, height)
    return {"type": "bbox", "class": str(labels[0]), "bbox": [x_min, y_min, x_max, y_max]}


def _polygon_shape(
    value: dict[str, Any], width: int | None, height: int | None, warnings: list[str]
) -> dict[str, Any] | None:
    labels = value.get("polygonlabels")
    if not isinstance(labels, list) or not labels:
        warnings.append("skipped polygon: missing 'polygonlabels'")
        return None
    if len(labels) > 1:
        warnings.append(f"polygon has multiple labels {labels!r}; using the first")

    points_raw = value.get("points")
    if not isinstance(points_raw, list):
        warnings.append("skipped polygon: missing 'points'")
        return None

    points: list[list[float]] = []
    for point in points_raw:
        if not isinstance(point, list) or len(point) != 2:
            warnings.append("skipped polygon: malformed point")
            return None
        points.append([_scale(float(point[0]), width), _scale(float(point[1]), height)])

    if len(points) < 3:
        warnings.append("skipped degenerate polygon: fewer than 3 points")
        return None

    return {"type": "polygon", "class": str(labels[0]), "points": points}


def _point_shape(
    value: dict[str, Any], width: int | None, height: int | None, warnings: list[str]
) -> dict[str, Any] | None:
    labels = value.get("keypointlabels")
    if not isinstance(labels, list) or not labels:
        warnings.append("skipped keypoint: missing 'keypointlabels'")
        return None
    if len(labels) > 1:
        warnings.append(f"keypoint has multiple labels {labels!r}; using the first")

    try:
        x = float(value["x"])
        y = float(value["y"])
    except (KeyError, TypeError, ValueError):
        warnings.append("skipped keypoint: missing or non-numeric x/y")
        return None

    point = [_scale(x, width), _scale(y, height)]
    return {"type": "point", "class": str(labels[0]), "point": point}


def _span_shape(value: dict[str, Any], warnings: list[str]) -> dict[str, Any] | None:
    """A `labels` (text NER) result: Unicode code-point `start`/`end` into the task text."""
    labels = value.get("labels")
    if not isinstance(labels, list) or not labels:
        warnings.append("skipped text span: missing 'labels'")
        return None
    if len(labels) > 1:
        warnings.append(f"text span has multiple labels {labels!r}; using the first")

    start = value.get("start")
    end = value.get("end")
    if not isinstance(start, int | float) or not isinstance(end, int | float):
        warnings.append("skipped text span: missing or non-numeric start/end")
        return None
    if int(end) <= int(start):
        warnings.append("skipped degenerate text span: end <= start")
        return None

    shape: dict[str, Any] = {
        "type": "span",
        "class": str(labels[0]),
        "start": int(start),
        "end": int(end),
    }
    text = value.get("text")
    if isinstance(text, str):
        shape["text"] = text
    return shape


def _relation_shape(
    result: dict[str, Any], id_map: dict[str, str], warnings: list[str]
) -> dict[str, Any] | None:
    """A `relation` result: `from_id`/`to_id` reference other results' `id`, not `value`."""
    from_id = result.get("from_id")
    to_id = result.get("to_id")
    if not isinstance(from_id, str) or not isinstance(to_id, str):
        warnings.append("skipped relation: missing 'from_id'/'to_id'")
        return None

    from_uuid = id_map.get(from_id)
    to_uuid = id_map.get(to_id)
    if from_uuid is None or to_uuid is None:
        warnings.append(f"skipped relation {from_id!r} -> {to_id!r}: unmatched region id")
        return None
    if from_uuid == to_uuid:
        warnings.append("skipped relation: 'from' and 'to' must differ")
        return None

    labels = result.get("labels")
    if not isinstance(labels, list) or not labels:
        warnings.append("skipped relation: missing 'labels'")
        return None
    if len(labels) > 1:
        warnings.append(f"relation has multiple labels {labels!r}; using the first")

    return {"type": "relation", "class": str(labels[0]), "from": from_uuid, "to": to_uuid}


def _brush_shape(
    value: dict[str, Any], width: int | None, height: int | None, warnings: list[str]
) -> dict[str, Any] | None:
    labels = value.get("brushlabels")
    if not isinstance(labels, list) or not labels:
        warnings.append("skipped brush mask: missing 'brushlabels'")
        return None
    if len(labels) > 1:
        warnings.append(f"brush mask has multiple labels {labels!r}; using the first")

    result_format = value.get("format", "rle")
    if result_format != "rle":
        warnings.append(f"skipped brush mask: unsupported format {result_format!r}")
        return None

    if width is None or height is None:
        warnings.append("skipped brush mask: missing image dimensions")
        return None

    rle = value.get("rle")
    if not isinstance(rle, list) or not rle:
        warnings.append("skipped brush mask: missing or empty 'rle'")
        return None

    try:
        counts = decode_label_studio_brush([int(v) for v in rle], width, height)
    except (ValueError, TypeError):
        warnings.append("skipped brush mask: malformed RLE encoding")
        return None

    if sum(counts) != width * height:
        warnings.append("skipped brush mask: decoded RLE counts do not match image size")
        return None

    return {
        "type": "mask",
        "class": str(labels[0]),
        "rle": {"size": [height, width], "counts": counts},
    }


def _choices_classification(
    result: dict[str, Any], value: dict[str, Any], warnings: list[str]
) -> tuple[str, Any] | None:
    from_name = str(result.get("from_name", "choices"))
    choices = value.get("choices")
    if not isinstance(choices, list) or not choices:
        warnings.append(f"skipped malformed 'choices' result for '{from_name}'")
        return None
    return from_name, (choices[0] if len(choices) == 1 else choices)


def _textarea_classification(
    result: dict[str, Any], value: dict[str, Any], warnings: list[str]
) -> tuple[str, Any] | None:
    from_name = str(result.get("from_name", "textarea"))
    text = value.get("text")
    if not isinstance(text, list) or not text:
        warnings.append(f"skipped malformed 'textarea' result for '{from_name}'")
        return None
    return from_name, "\n".join(str(part) for part in text)


def _process_result(
    result: dict[str, Any], width: int | None, height: int | None, warnings: list[str]
) -> tuple[dict[str, Any] | None, tuple[str, Any] | None]:
    """Convert one `result` entry into a shape dict and/or a classification entry."""
    result_type = result.get("type")
    value = result.get("value")
    if not isinstance(value, dict):
        warnings.append(f"skipped result: missing 'value' for type {result_type!r}")
        return None, None

    if result_type == "rectanglelabels":
        return _rectangle_shape(value, width, height, warnings), None
    if result_type == "polygonlabels":
        return _polygon_shape(value, width, height, warnings), None
    if result_type == "keypointlabels":
        return _point_shape(value, width, height, warnings), None
    if result_type == "brushlabels":
        return _brush_shape(value, width, height, warnings), None
    if result_type == "labels":
        return _span_shape(value, warnings), None
    if result_type == "choices":
        return None, _choices_classification(result, value, warnings)
    if result_type == "textarea":
        return None, _textarea_classification(result, value, warnings)

    if any(isinstance(value.get(key), list) for key in _LABEL_LIST_KEYS):
        warnings.append(f"skipped result of unsupported type {result_type!r}")
    else:
        warnings.append(f"skipped result of unrecognized type {result_type!r}")
    return None, None


@register_importer
class LabelStudioImporter:
    """Parses Label Studio task exports into imported items.

    Supports `rectanglelabels` (bbox), `polygonlabels` (polygon),
    `keypointlabels` (point), `brushlabels` (RLE mask, `format: "rle"` only),
    `labels` (text span), `relation` (between two regions already carrying a
    shape), `choices` and `textarea` (classification). Any other unrecognised
    result type is skipped with a warning.
    """

    format: ClassVar[str] = "label_studio"

    def parse(self, files: Iterable[ImportFile]) -> Iterator[ImportedItem]:
        json_files = [file for file in files if file.suffix == ".json"]
        if not json_files:
            raise ImportFormatError("no Label Studio JSON files found")
        for file in json_files:
            for task in _load_tasks(file):
                yield self._parse_task(task)

    def _parse_task(self, task: dict[str, Any]) -> ImportedItem:
        warnings: list[str] = []
        data = task.get("data")
        if not isinstance(data, dict):
            raise ImportFormatError(f"task {task.get('id')!r}: 'data' must be an object")

        path = _extract_path(data)
        if path is None:
            raise ImportFormatError(f"task {task.get('id')!r}: no path-like value found in 'data'")

        annotation = _select_annotation(task, warnings)
        if annotation is None:
            return ImportedItem(path=path, warnings=warnings)

        results = annotation.get("result")
        dict_results: list[dict[str, Any]] = []
        if isinstance(results, list):
            dict_results = [item for item in results if isinstance(item, dict)]

        width, height = _task_dimensions(dict_results)

        shapes: list[dict[str, Any]] = []
        classification: dict[str, Any] = {}
        id_map: dict[str, str] = {}
        relation_results: list[dict[str, Any]] = []
        for result in dict_results:
            if result.get("type") == "relation":
                # Relations reference other results' `id`, not a `value`; they need
                # every region processed first, so handle them in a second pass.
                relation_results.append(result)
                continue
            shape, classification_entry = _process_result(result, width, height, warnings)
            if shape is not None:
                region_id = result.get("id")
                if isinstance(region_id, str):
                    # Only regions a relation could reference need a stable id here;
                    # everything else keeps the job's own `setdefault(uuid4())`.
                    shape_id = str(uuid4())
                    shape["id"] = shape_id
                    id_map[region_id] = shape_id
                shapes.append(shape)
            if classification_entry is not None:
                key, entry_value = classification_entry
                classification[key] = entry_value

        for result in relation_results:
            relation = _relation_shape(result, id_map, warnings)
            if relation is not None:
                relation["id"] = str(uuid4())
                shapes.append(relation)

        coordinates: Coordinates
        if width is not None and height is not None:
            coordinates = "pixel"
        elif shapes and all(shape["type"] in ("span", "relation") for shape in shapes):
            # Text-only results (spans/relations) need no pixel scaling, so a
            # missing `original_width`/`original_height` is not an error here.
            coordinates = "pixel"
        else:
            coordinates = "normalized"

        return ImportedItem(
            path=path,
            width=width,
            height=height,
            coordinates=coordinates,
            shapes=shapes,
            classification=classification,
            warnings=warnings,
        )
