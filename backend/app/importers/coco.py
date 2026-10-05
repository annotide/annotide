"""COCO-format importer: detection (bbox), segmentation (polygon/mask) and keypoints.

Parses one or more COCO JSON documents (`images`, `annotations`, `categories`)
into :class:`ImportedItem` records. This is the inverse of
`app.exporters.coco.CocoExporter`; see that module for the JSON shapes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any, ClassVar

from app.importers.base import ImportedItem, ImportFile, ImportFormatError, register_importer
from app.importers.rle import decode_coco_compressed


def _iter_coco_documents(files: Iterable[ImportFile]) -> Iterator[dict[str, Any]]:
    """Parse every `.json` file that looks like a COCO document."""
    found = False
    for file in files:
        if file.suffix != ".json":
            continue
        try:
            payload = json.loads(file.data)
        except json.JSONDecodeError as exc:
            raise ImportFormatError(f"{file.path}: not valid JSON") from exc
        if not isinstance(payload, dict) or "images" not in payload or "annotations" not in payload:
            continue
        found = True
        yield payload
    if not found:
        raise ImportFormatError("no COCO-format JSON file found (missing 'images'/'annotations')")


def _segmentation_shapes(
    annotation: dict[str, Any], class_name: str, warnings: list[str]
) -> list[dict[str, Any]]:
    segmentation = annotation.get("segmentation")
    shapes: list[dict[str, Any]] = []

    if isinstance(segmentation, dict):
        size = segmentation.get("size")
        if not isinstance(size, list) or len(size) != 2:
            warnings.append("skipped mask: malformed RLE segmentation")
            return shapes
        height, width = int(size[0]), int(size[1])

        counts = segmentation.get("counts")
        decoded_counts: list[int]
        if isinstance(counts, str):
            try:
                decoded_counts = decode_coco_compressed(counts)
            except ValueError:
                warnings.append("skipped mask: malformed compressed RLE counts")
                return shapes
        elif isinstance(counts, list):
            decoded_counts = [int(c) for c in counts]
        else:
            warnings.append("skipped mask: malformed RLE segmentation")
            return shapes

        if sum(decoded_counts) != height * width:
            warnings.append("skipped mask: RLE counts do not sum to the mask size")
            return shapes

        shape: dict[str, Any] = {
            "type": "mask",
            "class": class_name,
            "rle": {"size": [height, width], "counts": decoded_counts},
        }
        attributes = annotation.get("attributes")
        if isinstance(attributes, dict):
            shape["attributes"] = attributes
        shapes.append(shape)
        return shapes

    if isinstance(segmentation, list) and segmentation:
        for ring in segmentation:
            if not isinstance(ring, list) or len(ring) < 6 or len(ring) % 2 != 0:
                warnings.append("skipped polygon: fewer than 3 points")
                continue
            points = [[float(ring[i]), float(ring[i + 1])] for i in range(0, len(ring), 2)]
            shape = {"type": "polygon", "class": class_name, "points": points}
            attributes = annotation.get("attributes")
            if isinstance(attributes, dict):
                shape["attributes"] = attributes
            shapes.append(shape)
        return shapes

    return shapes


def _keypoint_shapes(annotation: dict[str, Any], class_name: str) -> list[dict[str, Any]]:
    keypoints = annotation.get("keypoints")
    num_keypoints = annotation.get("num_keypoints")
    shapes: list[dict[str, Any]] = []
    if not keypoints or not num_keypoints or num_keypoints < 1:
        return shapes
    attributes = annotation.get("attributes")
    for i in range(0, len(keypoints), 3):
        x, y, visibility = keypoints[i], keypoints[i + 1], keypoints[i + 2]
        if visibility <= 0:
            continue
        shape: dict[str, Any] = {
            "type": "point",
            "class": class_name,
            "point": [float(x), float(y)],
        }
        if isinstance(attributes, dict):
            shape["attributes"] = attributes
        shapes.append(shape)
    return shapes


def _skeleton_shape(
    annotation: dict[str, Any], class_name: str, point_count: int, warnings: list[str]
) -> dict[str, Any] | None:
    """One `keypoints` shape for a category that names its keypoints; `None`
    when the annotation carries none, with a warning when the count is off."""
    keypoints = annotation.get("keypoints")
    if not isinstance(keypoints, list) or not keypoints:
        return None
    if len(keypoints) != 3 * point_count:
        warnings.append(
            f"skipped keypoints: {len(keypoints) // 3} points, the category names {point_count}"
        )
        return None
    points = [
        [float(keypoints[i]), float(keypoints[i + 1]), int(keypoints[i + 2])]
        if int(keypoints[i + 2]) > 0
        else [0.0, 0.0, 0]
        for i in range(0, len(keypoints), 3)
    ]
    if not any(v > 0 for _, _, v in points):
        return None
    shape: dict[str, Any] = {"type": "keypoints", "class": class_name, "points": points}
    attributes = annotation.get("attributes")
    if isinstance(attributes, dict):
        shape["attributes"] = attributes
    return shape


def _bbox_shape(
    annotation: dict[str, Any], class_name: str, warnings: list[str]
) -> dict[str, Any] | None:
    bbox = annotation.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        warnings.append("skipped annotation: missing or malformed bbox")
        return None
    x, y, width, height = (float(v) for v in bbox)
    if width <= 0 or height <= 0:
        warnings.append("skipped degenerate bbox: zero width or height")
        return None
    shape: dict[str, Any] = {
        "type": "bbox",
        "class": class_name,
        "bbox": [x, y, x + width, y + height],
    }
    attributes = annotation.get("attributes")
    if isinstance(attributes, dict):
        shape["attributes"] = attributes
    return shape


def _annotation_shapes(
    annotation: dict[str, Any],
    class_name: str,
    warnings: list[str],
    skeleton_size: int | None = None,
) -> list[dict[str, Any]]:
    if skeleton_size is not None:
        # A keypoints category (COCO person-keypoints layout): the skeleton is
        # the annotation's shape; its segmentation and bbox are left out.
        skeleton = _skeleton_shape(annotation, class_name, skeleton_size, warnings)
        if skeleton is not None:
            return [skeleton]

    segmentation = annotation.get("segmentation")
    if segmentation:
        # A segmentation is present: this is the annotation's shape, even if
        # every ring/RLE in it turns out to be degenerate (already warned).
        return _segmentation_shapes(annotation, class_name, warnings)

    keypoint_shapes = _keypoint_shapes(annotation, class_name)
    if keypoint_shapes:
        return keypoint_shapes

    bbox_shape = _bbox_shape(annotation, class_name, warnings)
    return [bbox_shape] if bbox_shape is not None else []


@register_importer
class CocoImporter:
    """Parses COCO-format detection/segmentation/keypoint JSON into imported items."""

    format: ClassVar[str] = "coco"

    def parse(self, files: Iterable[ImportFile]) -> Iterator[ImportedItem]:
        for document in _iter_coco_documents(files):
            categories = document.get("categories")
            if not isinstance(categories, list):
                raise ImportFormatError("COCO document is missing 'categories'")
            category_names: dict[int, str] = {}
            skeleton_sizes: dict[int, int] = {}
            for category in categories:
                if not isinstance(category, dict) or "id" not in category or "name" not in category:
                    raise ImportFormatError("COCO category is missing 'id' or 'name'")
                category_names[int(category["id"])] = str(category["name"])
                names = category.get("keypoints")
                if isinstance(names, list) and names:
                    skeleton_sizes[int(category["id"])] = len(names)

            images = document.get("images")
            if not isinstance(images, list):
                raise ImportFormatError("COCO document is missing 'images'")
            annotations = document.get("annotations")
            if not isinstance(annotations, list):
                raise ImportFormatError("COCO document is missing 'annotations'")

            annotations_by_image: dict[int, list[dict[str, Any]]] = {}
            for annotation in annotations:
                if not isinstance(annotation, dict) or "image_id" not in annotation:
                    raise ImportFormatError("COCO annotation is missing 'image_id'")
                annotations_by_image.setdefault(int(annotation["image_id"]), []).append(annotation)

            for image in images:
                if not isinstance(image, dict) or "id" not in image or "file_name" not in image:
                    raise ImportFormatError("COCO image is missing 'id' or 'file_name'")
                image_id = int(image["id"])
                width = image.get("width") or None
                height = image.get("height") or None
                warnings: list[str] = []
                shapes: list[dict[str, Any]] = []

                for annotation in annotations_by_image.get(image_id, []):
                    category_id = annotation.get("category_id")
                    if category_id is None or int(category_id) not in category_names:
                        raise ImportFormatError(
                            f"annotation {annotation.get('id')} references unknown category "
                            f"{category_id!r}"
                        )
                    class_name = category_names[int(category_id)]
                    shapes.extend(
                        _annotation_shapes(
                            annotation,
                            class_name,
                            warnings,
                            skeleton_sizes.get(int(category_id)),
                        )
                    )

                yield ImportedItem(
                    path=str(image["file_name"]),
                    width=int(width) if width else None,
                    height=int(height) if height else None,
                    coordinates="pixel",
                    shapes=shapes,
                    warnings=warnings,
                )
