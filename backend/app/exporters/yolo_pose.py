"""YOLO pose exporter: one label line per `keypoints` shape (TOOL, keypoint skeletons).

Ultralytics pose layout: `labels/<stem>.txt` lines `index cx cy w h x1 y1 v1 …`
normalised into `[0, 1]`, and a `data.yaml` with `kpt_shape: [K, 3]`. Pose
labels cannot share a file with detection lines, hence a format of its own
next to `yolo`. Only keypoints classes are indexed; every line carries `K`
points, the longest skeleton, and shorter skeletons are padded with `0 0 0`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import ClassVar

from app.exporters.base import (
    ExportFile,
    ExportItem,
    register_exporter,
    split_image_items,
    warnings_file,
)
from app.exporters.yolo import _UNKNOWN_DIMENSIONS_NOTE, _clamp, _label_stem
from app.schemas.annotation import KeypointsShape
from app.schemas.label_schema import ClassDef, LabelSchemaDefinition, ToolType

_SIDE = re.compile(r"left|right", re.IGNORECASE)


def _mirror_name(name: str) -> str:
    """`name` with every `left` and `right` swapped, keeping the case of each match."""

    def swap(match: re.Match[str]) -> str:
        word = match.group(0)
        other = "right" if word.lower() == "left" else "left"
        if word.isupper():
            return other.upper()
        if word[0].isupper():
            return other.capitalize()
        return other

    return _SIDE.sub(swap, name)


def flip_index(points: list[str], size: int) -> list[int]:
    """Each point's horizontal mirror by `left`/`right` in its name, else itself.

    Padded positions past `points` map to themselves.
    """
    position = {name.lower(): index for index, name in enumerate(points)}
    flipped = [position.get(_mirror_name(name).lower(), index) for index, name in enumerate(points)]
    return flipped + list(range(len(points), size))


def _pose_classes(definition: LabelSchemaDefinition) -> list[ClassDef]:
    return [
        cls
        for cls in definition.classes
        if ToolType.KEYPOINTS in cls.tools and cls.skeleton is not None
    ]


def _data_yaml(classes: list[ClassDef], size: int) -> str:
    lines = [f"kpt_shape: [{size}, 3]"]
    point_names = {tuple(cls.skeleton.points) for cls in classes if cls.skeleton is not None}
    if len(point_names) == 1:
        (names,) = point_names
        lines.append(f"flip_idx: [{', '.join(str(i) for i in flip_index(list(names), size))}]")
    lines.append(f"nc: {len(classes)}")
    lines.append("names:")
    lines.extend(f"  - {cls.name}" for cls in classes)
    return "\n".join(lines) + "\n"


@register_exporter
class YoloPoseExporter:
    """Exports `keypoints` shapes as YOLO pose label files plus data.yaml."""

    format: ClassVar[str] = "yolo_pose"

    def export(
        self, items: Iterable[ExportItem], definition: LabelSchemaDefinition
    ) -> Iterator[ExportFile]:
        classes = _pose_classes(definition)
        class_index = {cls.name: index for index, cls in enumerate(classes)}
        size = max((len(cls.skeleton.points) for cls in classes if cls.skeleton), default=0)
        image_items, warnings = split_image_items(items)
        if not classes:
            warnings.insert(0, "label schema has no keypoints class; nothing to export")

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
                if not isinstance(shape, KeypointsShape):
                    continue
                index = class_index.get(shape.class_)
                if index is None:
                    continue  # QA-6 rejects unknown classes before export; skip defensively
                x_min, y_min, x_max, y_max = shape.envelope()
                box = (
                    _clamp((x_min + x_max) / 2.0 / width),
                    _clamp((y_min + y_max) / 2.0 / height),
                    _clamp((x_max - x_min) / width),
                    _clamp((y_max - y_min) / height),
                )
                values = [f"{v:.6f}" for v in box]
                for x, y, visibility in shape.points[:size]:
                    if visibility == 0:
                        values.extend(("0", "0", "0"))
                    else:
                        values.extend(
                            (
                                f"{_clamp(x / width):.6f}",
                                f"{_clamp(y / height):.6f}",
                                str(visibility),
                            )
                        )
                values.extend(("0", "0", "0") * (size - min(len(shape.points), size)))
                lines.append(f"{index} {' '.join(values)}")

            content = ("\n".join(lines) + "\n") if lines else ""
            yield ExportFile(path=f"labels/{stem}.txt", data=content.encode("utf-8"))

        yield ExportFile(path="data.yaml", data=_data_yaml(classes, size).encode("utf-8"))
        warnings_export = warnings_file(warnings)
        if warnings_export is not None:
            yield warnings_export
