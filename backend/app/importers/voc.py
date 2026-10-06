"""Pascal VOC importer: one `<annotation>` XML file per image (EXP-6).

Uses the stdlib `xml.etree.ElementTree`. `ElementTree` does not resolve
external entities by default, which is enough for the trusted local files and
connector-fetched archives this importer sees; if uploads ever come from
untrusted third parties, swap in `defusedxml.ElementTree` instead.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, ClassVar
from xml.etree import ElementTree as ET

from app.importers.base import ImportedItem, ImportFile, ImportFormatError, register_importer


def _text(element: ET.Element, tag: str) -> str | None:
    child = element.find(tag)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _bool(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.strip() == "1"


def _parse_bndbox(obj: ET.Element, warnings: list[str], class_name: str) -> dict[str, Any] | None:
    bndbox = obj.find("bndbox")
    if bndbox is None:
        return None
    try:
        x_min = float(_text(bndbox, "xmin") or "")
        y_min = float(_text(bndbox, "ymin") or "")
        x_max = float(_text(bndbox, "xmax") or "")
        y_max = float(_text(bndbox, "ymax") or "")
    except ValueError:
        warnings.append(f"object '{class_name}': non-numeric bndbox, skipped")
        return None
    if x_max <= x_min or y_max <= y_min:
        warnings.append(f"object '{class_name}': degenerate bndbox, skipped")
        return None
    return {"type": "bbox", "class": class_name, "bbox": [x_min, y_min, x_max, y_max]}


def _parse_polygon(obj: ET.Element, warnings: list[str], class_name: str) -> dict[str, Any] | None:
    polygon = obj.find("polygon")
    if polygon is None:
        return None
    points: list[list[float]] = []
    for point in polygon.findall("point"):
        x_text = _text(point, "x")
        y_text = _text(point, "y")
        if x_text is None or y_text is None:
            continue
        try:
            points.append([float(x_text), float(y_text)])
        except ValueError:
            continue
    if len(points) < 3:
        warnings.append(f"object '{class_name}': polygon has fewer than 3 points, skipped")
        return None
    return {"type": "polygon", "class": class_name, "points": points}


def _object_attributes(obj: ET.Element) -> dict[str, Any]:
    attributes: dict[str, Any] = {}
    difficult = _bool(_text(obj, "difficult"))
    if difficult is not None:
        attributes["difficult"] = difficult
    truncated = _bool(_text(obj, "truncated"))
    if truncated is not None:
        attributes["truncated"] = truncated
    occluded = _bool(_text(obj, "occluded"))
    if occluded is not None:
        attributes["occluded"] = occluded
    pose = _text(obj, "pose")
    if pose is not None:
        attributes["pose"] = pose
    return attributes


@register_importer
class VocImporter:
    """Parses one Pascal VOC `<annotation>` XML file per image."""

    format: ClassVar[str] = "voc"

    def parse(self, files: Iterable[ImportFile]) -> Iterator[ImportedItem]:
        xml_files = [file for file in files if file.suffix == ".xml"]
        parsed_any = False
        for file in xml_files:
            try:
                root = ET.fromstring(file.data)
            except ET.ParseError as exc:
                raise ImportFormatError(f"{file.path}: not well-formed XML ({exc})") from exc
            if root.tag != "annotation":
                continue
            parsed_any = True
            yield self._parse_one(file, root)
        if not parsed_any:
            raise ImportFormatError("no Pascal VOC 'annotation' XML files found")

    def _parse_one(self, file: ImportFile, root: ET.Element) -> ImportedItem:
        warnings: list[str] = []

        path = _text(root, "filename")
        if path is None:
            path = file.stem
            warnings.append("missing <filename>, falling back to the XML file's stem")

        width: int | None = None
        height: int | None = None
        size = root.find("size")
        if size is not None:
            width_text = _text(size, "width")
            height_text = _text(size, "height")
            try:
                width = int(width_text) if width_text is not None else None
                height = int(height_text) if height_text is not None else None
            except ValueError:
                warnings.append("non-numeric <size>, dimensions left unknown")
                width = None
                height = None

        shapes: list[dict[str, Any]] = []
        for obj in root.findall("object"):
            class_name = _text(obj, "name")
            if class_name is None:
                warnings.append("object with no <name>, skipped")
                continue

            shape = _parse_bndbox(obj, warnings, class_name)
            if shape is None:
                shape = _parse_polygon(obj, warnings, class_name)
            if shape is None:
                warnings.append(f"object '{class_name}': no <bndbox> or <polygon>, skipped")
                continue

            attributes = _object_attributes(obj)
            if attributes:
                shape["attributes"] = attributes
            shapes.append(shape)

        return ImportedItem(
            path=path,
            width=width,
            height=height,
            coordinates="pixel",
            shapes=shapes,
            warnings=warnings,
        )
