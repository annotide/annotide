"""YOLO-format exporter: detect (bbox) and segment (polygon/polyline) label files.

One `.txt` file per image under `labels/`, plus a `data.yaml` listing class
names in index order (0-based, unlike COCO's 1-based category ids).
Coordinates are normalised into `[0, 1]` by image width/height and clamped.
Items with unknown dimensions are skipped without crashing; the skip is
recorded in that item's label file instead.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import PurePosixPath
from typing import ClassVar

from app.exporters.base import (
    ExportFile,
    ExportItem,
    register_exporter,
    split_image_items,
    warnings_file,
)
from app.schemas.annotation import BBoxShape, PolygonShape, PolylineShape, RBoxShape
from app.schemas.label_schema import LabelSchemaDefinition

_UNKNOWN_DIMENSIONS_NOTE = "# skipped: unknown image dimensions\n"


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _label_stem(path: str) -> str:
    return PurePosixPath(path).stem


@register_exporter
class YoloExporter:
    """Exports annotation results as YOLO detect/segment label files plus data.yaml."""

    format: ClassVar[str] = "yolo"

    def export(
        self, items: Iterable[ExportItem], definition: LabelSchemaDefinition
    ) -> Iterator[ExportFile]:
        class_index = {cls.name: index for index, cls in enumerate(definition.classes)}
        image_items, skip_warnings = split_image_items(items)

        for item in image_items:
            stem = _label_stem(item.path)

            if item.width is None or item.height is None:
                yield ExportFile(
                    path=f"labels/{stem}.txt", data=_UNKNOWN_DIMENSIONS_NOTE.encode("utf-8")
                )
                continue

            width, height = float(item.width), float(item.height)
            lines: list[str] = []
            for shape in item.result.shapes:
                index = class_index.get(shape.class_)
                if index is None:
                    continue  # QA-6 rejects unknown classes before export; skip defensively

                if isinstance(shape, BBoxShape):
                    x_min, y_min, x_max, y_max = shape.bbox
                    center_x = _clamp(((x_min + x_max) / 2.0) / width)
                    center_y = _clamp(((y_min + y_max) / 2.0) / height)
                    box_width = _clamp((x_max - x_min) / width)
                    box_height = _clamp((y_max - y_min) / height)
                    lines.append(
                        f"{index} {center_x:.6f} {center_y:.6f} {box_width:.6f} {box_height:.6f}"
                    )
                elif isinstance(shape, PolygonShape | PolylineShape | RBoxShape):
                    # A rotated box is written as its four corners, which is
                    # exactly the YOLO OBB label format.
                    points = shape.corners() if isinstance(shape, RBoxShape) else shape.points
                    coords = " ".join(
                        f"{_clamp(x / width):.6f} {_clamp(y / height):.6f}" for x, y in points
                    )
                    lines.append(f"{index} {coords}")
                # point, mask and keypoints shapes are skipped: YOLO pose labels are a
                # different file layout that cannot share a file with detection lines

            content = ("\n".join(lines) + "\n") if lines else ""
            yield ExportFile(path=f"labels/{stem}.txt", data=content.encode("utf-8"))

        names_lines = "\n".join(f"  - {cls.name}" for cls in definition.classes)
        yaml_content = f"nc: {len(definition.classes)}\nnames:\n{names_lines}\n"
        yield ExportFile(path="data.yaml", data=yaml_content.encode("utf-8"))
        warnings_export = warnings_file(skip_warnings)
        if warnings_export is not None:
            yield warnings_export
