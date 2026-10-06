"""YOLO-format importer: detect (bbox) and segment (polygon) label files.

Parses a `data.yaml` (class names) plus `*.txt` label files (one per image,
normalised `[0, 1]` coordinates) into :class:`ImportedItem` records. This is
the inverse of `app.exporters.yolo.YoloExporter`; see that module for the
line formats. Only the small subset of YAML the exporter writes (a `names`
block/inline list or index mapping, plus `nc`) is parsed — no `pyyaml`
dependency.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import Any, ClassVar

from app.importers.base import ImportedItem, ImportFile, ImportFormatError, register_importer

_NAMES_KEY_RE = re.compile(r"^(\s*)names\s*:\s*(.*)$")
_MAPPING_ENTRY_RE = re.compile(r"^(\d+)\s*:\s*(.+)$")
_NO_NAMES_WARNING = "class names unavailable: no data.yaml found, using numeric indices"


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _parse_inline_list(remainder: str) -> dict[int, str]:
    body = remainder.strip().removeprefix("[").removesuffix("]")
    names = [_strip_quotes(part) for part in body.split(",") if part.strip()]
    return dict(enumerate(names))


def _parse_block(lines: list[str], start: int, key_indent: int) -> dict[int, str]:
    names: dict[int, str] = {}
    index = 0
    for line in lines[start:]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= key_indent:
            break
        content = line.strip()
        mapping_match = _MAPPING_ENTRY_RE.match(content)
        if content.startswith("- "):
            names[index] = _strip_quotes(content[2:])
            index += 1
        elif mapping_match:
            names[int(mapping_match.group(1))] = _strip_quotes(mapping_match.group(2))
        # anything else (comments, unrecognised keys) is ignored
    return names


def _parse_yaml_names(text: str) -> dict[int, str] | None:
    """Extract a `names` list/mapping from a YOLO `data.yaml`-style document."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = _NAMES_KEY_RE.match(line)
        if match is None:
            continue
        key_indent, remainder = match.groups()
        if remainder.strip():
            return _parse_inline_list(remainder)
        return _parse_block(lines, i + 1, len(key_indent))
    return None


def _find_class_names(files: Iterable[ImportFile]) -> dict[int, str] | None:
    for file in files:
        if file.suffix not in {".yaml", ".yml"}:
            continue
        try:
            text = file.data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        names = _parse_yaml_names(text)
        if names:
            return names
    return None


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _parse_line(
    line: str, class_names: dict[int, str] | None, warnings: list[str]
) -> dict[str, Any] | None:
    tokens = line.split()
    try:
        class_index = int(tokens[0])
        coords = [float(t) for t in tokens[1:]]
    except (ValueError, IndexError):
        warnings.append(f"skipped malformed line: {line!r}")
        return None

    if class_names is not None:
        class_name = class_names.get(class_index)
        if class_name is None:
            warnings.append(f"skipped line with unknown class index {class_index}")
            return None
    else:
        class_name = str(class_index)

    if len(coords) == 4:
        cx, cy, w, h = coords
        bbox = [
            _clamp(cx - w / 2.0),
            _clamp(cy - h / 2.0),
            _clamp(cx + w / 2.0),
            _clamp(cy + h / 2.0),
        ]
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            warnings.append("skipped degenerate bbox: zero width or height")
            return None
        return {"type": "bbox", "class": class_name, "bbox": bbox}

    if len(coords) == 5:
        cx, cy, w, h, confidence = coords
        bbox = [
            _clamp(cx - w / 2.0),
            _clamp(cy - h / 2.0),
            _clamp(cx + w / 2.0),
            _clamp(cy + h / 2.0),
        ]
        if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
            warnings.append("skipped degenerate bbox: zero width or height")
            return None
        return {
            "type": "bbox",
            "class": class_name,
            "bbox": bbox,
            "confidence": confidence,
        }

    if len(coords) >= 6 and len(coords) % 2 == 0:
        points = [[coords[i], coords[i + 1]] for i in range(0, len(coords), 2)]
        return {"type": "polygon", "class": class_name, "points": points}

    warnings.append(f"skipped malformed line: {line!r}")
    return None


@register_importer
class YoloImporter:
    """Parses YOLO-format detect/segment label files into imported items."""

    format: ClassVar[str] = "yolo"

    def parse(self, files: Iterable[ImportFile]) -> Iterator[ImportedItem]:
        files = list(files)
        class_names = _find_class_names(files)
        label_files = [file for file in files if file.suffix == ".txt"]
        if not label_files:
            raise ImportFormatError("no YOLO label files (*.txt) found")

        for file in label_files:
            warnings: list[str] = []
            if class_names is None:
                warnings.append(_NO_NAMES_WARNING)

            shapes: list[dict[str, Any]] = []
            try:
                text = file.data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ImportFormatError(f"{file.path}: not valid UTF-8 text") from exc

            for raw_line in text.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                shape = _parse_line(line, class_names, warnings)
                if shape is not None:
                    shapes.append(shape)

            yield ImportedItem(
                path=file.stem,
                width=None,
                height=None,
                coordinates="normalized",
                shapes=shapes,
                warnings=warnings,
            )
