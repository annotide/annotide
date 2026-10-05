"""CVAT XML 1.1 "images" and "video" export importer (EXP-6).

Uses the stdlib `xml.etree.ElementTree`. `ElementTree` does not resolve
external entities by default, which is enough for the trusted local files and
connector-fetched archives this importer sees; if uploads ever come from
untrusted third parties, swap in `defusedxml.ElementTree` instead.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, ClassVar
from uuid import uuid4
from xml.etree import ElementTree as ET

from app.importers.base import ImportedItem, ImportFile, ImportFormatError, register_importer

_UNSUPPORTED_SHAPES = {"mask", "ellipse", "cuboid", "skeleton"}
_TRACK_SHAPE_TAGS = {"box", "polygon", "polyline", "points"}


def _coerce(value: str) -> Any:
    """`"true"`/`"false"` become booleans, every other string is kept as-is."""
    if value == "true":
        return True
    if value == "false":
        return False
    return value


def _parse_points(raw: str) -> list[list[float]]:
    points: list[list[float]] = []
    for pair in raw.strip().split(";"):
        if not pair:
            continue
        x_text, _, y_text = pair.partition(",")
        points.append([float(x_text), float(y_text)])
    return points


def _element_attributes(element: ET.Element) -> dict[str, Any]:
    """`<attribute name="...">value</attribute>` children as a flat dict."""
    attributes: dict[str, Any] = {}
    for attribute in element.findall("attribute"):
        name = attribute.get("name")
        if name is None:
            continue
        attributes[name] = _coerce((attribute.text or "").strip())
    return attributes


def _shape_attributes(element: ET.Element) -> dict[str, Any]:
    attributes = _element_attributes(element)
    if element.get("occluded") == "1":
        attributes.setdefault("occluded", True)
    return attributes


@register_importer
class CvatImporter:
    """Parses a CVAT XML 1.1 "images" (`<image>`) or "video" (`<track>`) export.

    A track-based file yields exactly one `ImportedItem` for the whole video,
    `media_type="video"`, one `track_id` per `<track>`, keyframe and `outside`
    shapes kept, interpolated in-between frames dropped (the player derives
    them). Mixing image and track files in one import works: each XML file is
    parsed independently.
    """

    format: ClassVar[str] = "cvat"

    def parse(self, files: Iterable[ImportFile]) -> Iterator[ImportedItem]:
        xml_files = [file for file in files if file.suffix == ".xml"]
        parsed_any = False
        for file in xml_files:
            try:
                root = ET.fromstring(file.data)
            except ET.ParseError as exc:
                raise ImportFormatError(f"{file.path}: not well-formed XML ({exc})") from exc
            if root.tag != "annotations":
                continue
            parsed_any = True
            if root.find("track") is not None:
                yield self._parse_video(root)
                continue
            for image in root.findall("image"):
                yield self._parse_image(image)
        if not parsed_any:
            raise ImportFormatError("no CVAT 'annotations' XML files found")

    def _parse_video(self, root: ET.Element) -> ImportedItem:
        """A CVAT "for video 1.1" export: one `<track>` per object, `<box>`/etc per frame."""
        warnings: list[str] = []
        task = root.find("meta/task")
        path = ""
        width: int | None = None
        height: int | None = None
        meta: dict[str, Any] = {}
        if task is not None:
            path = task.findtext("source") or task.findtext("name") or ""
            size_text = task.findtext("size")
            if size_text is not None:
                try:
                    meta["frame_count"] = int(size_text)
                except ValueError:
                    warnings.append(f"task size {size_text!r} is not an integer, ignored")
            fps_text = task.findtext("fps")
            if fps_text is not None:
                try:
                    meta["fps"] = float(fps_text)
                except ValueError:
                    warnings.append(f"task fps {fps_text!r} is not a number, ignored")
            original_size = task.find("original_size")
            if original_size is not None:
                width_text = original_size.findtext("width")
                height_text = original_size.findtext("height")
                width = int(width_text) if width_text is not None else None
                height = int(height_text) if height_text is not None else None

        shapes: list[dict[str, Any]] = []
        for track in root.findall("track"):
            label = track.get("label", "")
            track_id = str(uuid4())
            for child in track:
                if child.tag not in _TRACK_SHAPE_TAGS:
                    if child.tag in _UNSUPPORTED_SHAPES:
                        warnings.append(f"'{child.tag}' shapes are not supported, skipped")
                    continue

                keyframe = child.get("keyframe") == "1"
                outside = child.get("outside") == "1"
                if not (keyframe or outside):
                    continue  # interpolated frame: derived by the player, not stored

                frame_text = child.get("frame")
                if frame_text is None:
                    warnings.append(f"track {track_id} '{label}': missing frame, skipped")
                    continue
                frame = int(frame_text)

                track_shapes: list[dict[str, Any]]
                if child.tag == "box":
                    shape = self._parse_box(child, label, warnings)
                    track_shapes = [shape] if shape is not None else []
                elif child.tag == "polygon":
                    shape = self._parse_polygon(child, label, warnings)
                    track_shapes = [shape] if shape is not None else []
                elif child.tag == "polyline":
                    shape = self._parse_polyline(child, label, warnings)
                    track_shapes = [shape] if shape is not None else []
                else:
                    track_shapes = self._parse_points_shape(child, label, warnings)

                for track_shape in track_shapes:
                    track_shape["frame"] = frame
                    track_shape["track_id"] = track_id
                    track_shape["keyframe"] = keyframe
                    track_shape["outside"] = outside
                    shapes.append(track_shape)

        return ImportedItem(
            path=path,
            width=width,
            height=height,
            coordinates="pixel",
            shapes=shapes,
            media_type="video",
            meta=meta,
            warnings=warnings,
        )

    def _parse_image(self, image: ET.Element) -> ImportedItem:
        warnings: list[str] = []
        path = image.get("name", "")
        width_text = image.get("width")
        height_text = image.get("height")
        width = int(width_text) if width_text is not None else None
        height = int(height_text) if height_text is not None else None

        shapes: list[dict[str, Any]] = []
        classification: dict[str, Any] = {}

        for child in image:
            tag = child.tag
            label = child.get("label", "")
            if tag == "box":
                shape = self._parse_box(child, label, warnings)
                if shape is not None:
                    shapes.append(shape)
            elif tag == "polygon":
                shape = self._parse_polygon(child, label, warnings)
                if shape is not None:
                    shapes.append(shape)
            elif tag == "polyline":
                shape = self._parse_polyline(child, label, warnings)
                if shape is not None:
                    shapes.append(shape)
            elif tag == "points":
                shapes.extend(self._parse_points_shape(child, label, warnings))
            elif tag == "tag":
                classification[label] = True
                for name, value in _element_attributes(child).items():
                    classification[f"{label}.{name}"] = value
            elif tag in _UNSUPPORTED_SHAPES:
                warnings.append(f"'{tag}' shapes are not supported, skipped")
            # else: unknown element, ignore

        return ImportedItem(
            path=path,
            width=width,
            height=height,
            coordinates="pixel",
            shapes=shapes,
            classification=classification,
            warnings=warnings,
        )

    def _parse_box(
        self, element: ET.Element, label: str, warnings: list[str]
    ) -> dict[str, Any] | None:
        try:
            x_min = float(element.get("xtl", ""))
            y_min = float(element.get("ytl", ""))
            x_max = float(element.get("xbr", ""))
            y_max = float(element.get("ybr", ""))
        except ValueError:
            warnings.append(f"box '{label}': non-numeric coordinates, skipped")
            return None
        if x_max <= x_min or y_max <= y_min:
            warnings.append(f"box '{label}': degenerate bbox, skipped")
            return None
        shape: dict[str, Any] = {
            "type": "bbox",
            "class": label,
            "bbox": [x_min, y_min, x_max, y_max],
        }
        attributes = _shape_attributes(element)
        if attributes:
            shape["attributes"] = attributes
        return shape

    def _parse_polygon(
        self, element: ET.Element, label: str, warnings: list[str]
    ) -> dict[str, Any] | None:
        points = _parse_points(element.get("points", ""))
        if len(points) < 3:
            warnings.append(f"polygon '{label}': fewer than 3 points, skipped")
            return None
        shape: dict[str, Any] = {"type": "polygon", "class": label, "points": points}
        attributes = _shape_attributes(element)
        if attributes:
            shape["attributes"] = attributes
        return shape

    def _parse_polyline(
        self, element: ET.Element, label: str, warnings: list[str]
    ) -> dict[str, Any] | None:
        points = _parse_points(element.get("points", ""))
        if len(points) < 2:
            warnings.append(f"polyline '{label}': fewer than 2 points, skipped")
            return None
        shape: dict[str, Any] = {"type": "polyline", "class": label, "points": points}
        attributes = _shape_attributes(element)
        if attributes:
            shape["attributes"] = attributes
        return shape

    def _parse_points_shape(
        self, element: ET.Element, label: str, warnings: list[str]
    ) -> list[dict[str, Any]]:
        points = _parse_points(element.get("points", ""))
        if not points:
            warnings.append(f"points '{label}': no points, skipped")
            return []
        attributes = _shape_attributes(element)
        shapes = []
        for point in points:
            shape: dict[str, Any] = {"type": "point", "class": label, "point": point}
            if attributes:
                shape["attributes"] = dict(attributes)
            shapes.append(shape)
        return shapes
