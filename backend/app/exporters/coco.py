"""COCO-format exporter: detection (bbox), segmentation (polygon/mask) and keypoints.

Converts the contract's `[x_min, y_min, x_max, y_max]` bbox into COCO's
`[x, y, width, height]`, flattens polygons into COCO's `[[x1, y1, x2, y2, ...]]`
segmentation form, and assigns 1-based category ids from the label schema's
class order. A `keypoints` shape becomes a COCO keypoints annotation and its
class's category carries the skeleton's names and 1-based bones; a lone
`point` shape is a one-keypoint annotation.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any, ClassVar

from app.exporters.base import (
    ExportFile,
    ExportItem,
    register_exporter,
    split_image_items,
    warnings_file,
)
from app.schemas.annotation import (
    BBoxShape,
    KeypointsShape,
    MaskShape,
    PointShape,
    PolygonShape,
    PolylineShape,
    RBoxShape,
)
from app.schemas.label_schema import LabelSchemaDefinition

Coord = tuple[float, float]


def _bbox_to_xywh(bbox: tuple[float, float, float, float]) -> tuple[list[float], float]:
    """Convert `[x_min, y_min, x_max, y_max]` to COCO's `[x, y, width, height]` plus area."""
    x_min, y_min, x_max, y_max = bbox
    width = x_max - x_min
    height = y_max - y_min
    return [x_min, y_min, width, height], width * height


def _polygon_area(points: list[Coord]) -> float:
    """Shoelace formula for the area of a simple (non-self-intersecting) polygon."""
    total = 0.0
    count = len(points)
    for i in range(count):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % count]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _bounding_box(points: list[Coord]) -> tuple[list[float], float]:
    """Axis-aligned `[x, y, width, height]` bounding box enclosing `points`, plus its area."""
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    x_min, y_min = min(xs), min(ys)
    width, height = max(xs) - x_min, max(ys) - y_min
    return [x_min, y_min, width, height], width * height


def _flatten(points: list[Coord]) -> list[float]:
    return [coordinate for point in points for coordinate in point]


def _mask_area(counts: list[int]) -> float:
    """Sum of foreground run lengths in an uncompressed RLE (odd-indexed runs)."""
    return float(sum(counts[1::2]))


@register_exporter
class CocoExporter:
    """Exports annotation results as a single COCO-format JSON file."""

    format: ClassVar[str] = "coco"

    def export(
        self, items: Iterable[ExportItem], definition: LabelSchemaDefinition
    ) -> Iterator[ExportFile]:
        image_items, skip_warnings = split_image_items(items)

        category_ids = {cls.name: index + 1 for index, cls in enumerate(definition.classes)}
        categories: list[dict[str, Any]] = []
        for cls in definition.classes:
            category: dict[str, Any] = {
                "id": category_ids[cls.name],
                "name": cls.name,
                "supercategory": "none",
            }
            if cls.skeleton is not None:
                # COCO's person-keypoints layout: names, and 1-based bone pairs.
                category["keypoints"] = list(cls.skeleton.points)
                category["skeleton"] = [[a + 1, b + 1] for a, b in cls.skeleton.edges]
            categories.append(category)

        images: list[dict[str, Any]] = []
        annotations: list[dict[str, Any]] = []
        annotation_id = 1

        for image_id, item in enumerate(image_items, start=1):
            images.append(
                {
                    "id": image_id,
                    "file_name": item.path,
                    "width": item.width or 0,
                    "height": item.height or 0,
                }
            )
            for shape in item.result.shapes:
                category_id = category_ids.get(shape.class_)
                if category_id is None:
                    continue  # QA-6 rejects unknown classes before export; skip defensively
                record: dict[str, Any] = {
                    "id": annotation_id,
                    "image_id": image_id,
                    "category_id": category_id,
                    "iscrowd": 0,
                }
                if isinstance(shape, BBoxShape):
                    bbox, area = _bbox_to_xywh(shape.bbox)
                    record["bbox"] = bbox
                    record["area"] = area
                    record["segmentation"] = []
                elif isinstance(shape, RBoxShape):
                    # COCO has no rotated box: the corners become the segmentation
                    # and the axis-aligned envelope the bbox.
                    corners = shape.corners()
                    bbox, _ = _bounding_box(corners)
                    record["bbox"] = bbox
                    record["area"] = shape.size[0] * shape.size[1]
                    record["segmentation"] = [_flatten(corners)]
                elif isinstance(shape, PolygonShape):
                    bbox, _ = _bounding_box(shape.points)
                    record["bbox"] = bbox
                    record["area"] = _polygon_area(shape.points)
                    record["segmentation"] = [_flatten(shape.points)]
                elif isinstance(shape, PolylineShape):
                    bbox, bbox_area = _bounding_box(shape.points)
                    record["bbox"] = bbox
                    record["area"] = bbox_area
                    record["segmentation"] = [_flatten(shape.points)]
                elif isinstance(shape, PointShape):
                    x, y = shape.point
                    record["bbox"] = [x, y, 0.0, 0.0]
                    record["area"] = 0.0
                    record["segmentation"] = []
                    record["keypoints"] = [x, y, 2]
                    record["num_keypoints"] = 1
                elif isinstance(shape, KeypointsShape):
                    bbox, bbox_area = _bounding_box(shape.labelled())
                    record["bbox"] = bbox
                    record["area"] = bbox_area
                    record["segmentation"] = []
                    record["keypoints"] = [
                        value
                        for x, y, v in shape.points
                        for value in ((x, y, v) if v > 0 else (0, 0, 0))
                    ]
                    record["num_keypoints"] = sum(1 for _, _, v in shape.points if v > 0)
                elif isinstance(shape, MaskShape):
                    height, width = shape.rle.size
                    record["bbox"] = [0.0, 0.0, float(width), float(height)]
                    record["area"] = _mask_area(shape.rle.counts)
                    record["segmentation"] = {
                        "size": list(shape.rle.size),
                        "counts": shape.rle.counts,
                    }
                annotations.append(record)
                annotation_id += 1

        payload = {"images": images, "annotations": annotations, "categories": categories}
        yield ExportFile(path="coco.json", data=json.dumps(payload, indent=2).encode("utf-8"))
        warnings_export = warnings_file(skip_warnings)
        if warnings_export is not None:
            yield warnings_export
