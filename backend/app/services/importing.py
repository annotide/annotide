"""Dataset import: reading the source files, matching records to items, building results (EXP-6).

The importers in `app.importers` are pure parsers that know nothing about
the project. Everything that needs the database or the label schema — which
item a record belongs to, which schema class a source class maps to, whether
the result validates — lives here so the job body in `app.worker.jobs` stays
a thin loop and every rule is unit-testable on its own.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from pydantic import ValidationError

from app.connectors.base import StorageConnector
from app.importers import ImportedItem, ImportFile, ImportFormatError, expand_archive, is_archive
from app.models import Item
from app.schemas import AnnotationResult, LabelSchemaDefinition

#: How many unmatched paths / problem messages the job result keeps. The
#: counts are exact; the samples are there to make the first fix obvious.
RESULT_SAMPLE_SIZE = 20

#: Upper bound on one import's input, before and after unpacking. Annotation
#: files are small; anything larger is almost certainly media by mistake.
MAX_IMPORT_BYTES = 64 * 1024 * 1024


async def read_import_files(storage: StorageConnector, path: str) -> list[ImportFile]:
    """Load the import's input from a connector.

    `path` is a zip archive (expanded in memory), a prefix ending in `/`
    (every object under it) or a single file. Paths inside the result are
    relative to the archive root / the prefix, which is what the importers
    expect (`labels/x.txt`, `data.yaml`, …).
    """
    if path.endswith("/"):
        files: list[ImportFile] = []
        total = 0
        async for info in storage.list(path):
            data = await storage.read(info.path)
            total += len(data)
            if total > MAX_IMPORT_BYTES:
                raise ImportFormatError(f"import exceeds {MAX_IMPORT_BYTES} bytes under {path!r}")
            relative = info.path[len(path) :] if info.path.startswith(path) else info.path
            files.append(ImportFile(path=relative, data=data))
        if not files:
            raise ImportFormatError(f"no files found under {path!r}")
        return files

    data = await storage.read(path)
    if len(data) > MAX_IMPORT_BYTES:
        raise ImportFormatError(f"import file {path!r} exceeds {MAX_IMPORT_BYTES} bytes")
    if is_archive(path):
        files = expand_archive(data, max_uncompressed_bytes=MAX_IMPORT_BYTES)
        if sum(len(f.data) for f in files) > MAX_IMPORT_BYTES:
            raise ImportFormatError(f"archive {path!r} exceeds {MAX_IMPORT_BYTES} bytes unpacked")
        if not files:
            raise ImportFormatError(f"archive {path!r} is empty")
        return files
    return [ImportFile(path=PurePosixPath(path).name, data=data)]


@dataclass(slots=True)
class ItemIndex:
    """Project items keyed three ways, for resolving the paths a source names.

    Basename and stem lookups only resolve when unique: two `imgs/a.jpg` and
    `other/a.jpg` make `a.jpg` ambiguous, and an ambiguous match is worse
    than no match at all.
    """

    by_path: dict[str, Item] = field(default_factory=dict)
    by_name: dict[str, Item | None] = field(default_factory=dict)
    by_stem: dict[str, Item | None] = field(default_factory=dict)

    @classmethod
    def build(cls, items: Sequence[Item]) -> ItemIndex:
        index = cls()
        for item in items:
            index.by_path[item.path] = item
            posix = PurePosixPath(item.path)
            for table, key in ((index.by_name, posix.name), (index.by_stem, posix.stem)):
                table[key] = None if key in table else item
        return index

    def resolve(self, path: str) -> Item | None:
        """Exact path, then unique basename, then unique stem (YOLO gives only the stem)."""
        cleaned = path.strip().lstrip("./")
        if cleaned in self.by_path:
            return self.by_path[cleaned]
        posix = PurePosixPath(cleaned)
        hit = self.by_name.get(posix.name)
        if hit is not None:
            return hit
        stem = posix.stem if posix.suffix else cleaned
        return self.by_stem.get(stem)


@dataclass(slots=True)
class ConvertedItem:
    """The outcome of turning one imported record into an annotation result."""

    result: AnnotationResult | None
    dropped_shapes: int
    problems: list[str]
    dropped_attributes: int = 0


#: `{schema_class | "*": {source_attribute: schema_attribute | None}}` (CONTRACTS `import`).
AttributeMapping = Mapping[str, Mapping[str, str | None]]


def _map_attributes(
    attributes: Mapping[str, Any],
    target_class: str,
    declared: set[str],
    attribute_mapping: AttributeMapping,
) -> tuple[dict[str, Any], int]:
    """Rename a shape's attributes for its mapped class; return them and the dropped count.

    A per-class entry wins over `"*"` key by key; `None` discards. Whatever
    the class does not declare afterwards is dropped rather than failing the
    item, like an unknown class.
    """
    rules = {**attribute_mapping.get("*", {}), **attribute_mapping.get(target_class, {})}
    kept: dict[str, Any] = {}
    dropped = 0
    for name, value in attributes.items():
        target = rules.get(name, name)
        if target is None or target not in declared:
            dropped += 1
            continue
        kept[target] = value
    return kept, dropped


def _scale(value: Any, factor: float) -> float:
    return float(value) * factor


def _denormalise(shape: dict[str, Any], width: float, height: float) -> dict[str, Any]:
    scaled = dict(shape)
    if "bbox" in shape:
        x_min, y_min, x_max, y_max = shape["bbox"]
        scaled["bbox"] = [
            _scale(x_min, width),
            _scale(y_min, height),
            _scale(x_max, width),
            _scale(y_max, height),
        ]
    if "points" in shape:
        # `[x, y]`, or `[x, y, v]` for keypoints (visibility is not a coordinate)
        scaled["points"] = [
            [_scale(p[0], width), _scale(p[1], height), *p[2:]] for p in shape["points"]
        ]
    if "point" in shape:
        x, y = shape["point"]
        scaled["point"] = [_scale(x, width), _scale(y, height)]
    return scaled


def convert_item(
    imported: ImportedItem,
    item: Item,
    definition: LabelSchemaDefinition,
    class_mapping: Mapping[str, str],
    *,
    schema_version: int,
    attribute_mapping: AttributeMapping | None = None,
) -> ConvertedItem:
    """Map classes, bring coordinates to pixels and validate into an `AnnotationResult`.

    Shapes whose (mapped) class is not in the label schema are dropped and
    counted, never failed: a partial import with a clear count beats an
    import that stops at the first unknown class. The same goes for shape
    attributes the mapped class does not declare (after `attribute_mapping`).
    Anything else the shape validators reject is a problem for the whole item.
    """
    declared = {cls.name: {attr.name for attr in cls.attributes} for cls in definition.classes}
    known = set(declared)
    skeleton_sizes = {
        cls.name: len(cls.skeleton.points) for cls in definition.classes if cls.skeleton
    }
    attr_rules: AttributeMapping = attribute_mapping or {}
    dropped_attributes = 0
    problems: list[str] = list(imported.warnings)

    if imported.media_type is not None and imported.media_type != str(item.media_type):
        problems.append(
            f"{imported.path}: source is '{imported.media_type}' but the matched item is "
            f"'{item.media_type}', skipped"
        )
        return ConvertedItem(None, 0, problems)

    width = item.width or imported.width
    height = item.height or imported.height
    if imported.coordinates == "normalized" and not (width and height):
        problems.append(
            f"{imported.path}: normalised coordinates but the image dimensions are unknown"
        )
        return ConvertedItem(None, 0, problems)

    shapes: list[dict[str, Any]] = []
    dropped = 0
    for raw in imported.shapes:
        source_class = str(raw.get("class", ""))
        mapped = class_mapping.get(source_class, source_class)
        if mapped not in known:
            dropped += 1
            continue
        if raw.get("type") == "keypoints":
            points = raw.get("points")
            size = skeleton_sizes.get(mapped)
            if size is None or not isinstance(points, list) or len(points) != size:
                # The target class has no skeleton, or one of another size.
                dropped += 1
                continue
        shape = dict(raw)
        shape["class"] = mapped
        raw_attributes = raw.get("attributes")
        if isinstance(raw_attributes, Mapping) and raw_attributes:
            shape["attributes"], lost = _map_attributes(
                raw_attributes, mapped, declared[mapped], attr_rules
            )
            dropped_attributes += lost
        shape.setdefault("id", str(uuid.uuid4()))
        if imported.coordinates == "normalized":
            shape = _denormalise(shape, float(width or 0), float(height or 0))
        shapes.append(shape)

    try:
        result = AnnotationResult.model_validate(
            {
                "schema_version": schema_version,
                "media_type": str(item.media_type),
                "classification": imported.classification,
                "shapes": shapes,
            }
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        problems.append(f"{imported.path}: {location}: {first['msg']}")
        return ConvertedItem(None, dropped, problems, dropped_attributes)
    return ConvertedItem(result, dropped, problems, dropped_attributes)


@dataclass(slots=True)
class ImportTally:
    """Running counts for the job result (see CONTRACTS `import`)."""

    parsed: int = 0
    matched: int = 0
    unmatched: int = 0
    imported: int = 0
    errors: int = 0
    dropped_shapes: int = 0
    dropped_attributes: int = 0
    classes: Counter[str] = field(default_factory=Counter)
    #: Source attribute names per source class, before mapping (for the UI's mapping step).
    attributes: dict[str, set[str]] = field(default_factory=dict)
    unmatched_sample: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def note_shape(self, shape: Mapping[str, Any]) -> None:
        source_class = str(shape.get("class", ""))
        self.classes[source_class] += 1
        attributes = shape.get("attributes")
        if isinstance(attributes, Mapping) and attributes:
            self.attributes.setdefault(source_class, set()).update(str(k) for k in attributes)

    def note_unmatched(self, path: str) -> None:
        self.unmatched += 1
        if len(self.unmatched_sample) < RESULT_SAMPLE_SIZE:
            self.unmatched_sample.append(path)

    def note_problems(self, messages: Sequence[str]) -> None:
        room = RESULT_SAMPLE_SIZE - len(self.problems)
        if room > 0:
            self.problems.extend(messages[:room])

    def as_result(self, *, import_format: str, dry_run: bool) -> dict[str, Any]:
        return {
            "format": import_format,
            "dry_run": dry_run,
            "parsed": self.parsed,
            "matched": self.matched,
            "unmatched": self.unmatched,
            "imported": self.imported,
            "errors": self.errors,
            "dropped_shapes": self.dropped_shapes,
            "dropped_attributes": self.dropped_attributes,
            "classes": dict(sorted(self.classes.items())),
            "attributes": {name: sorted(seen) for name, seen in sorted(self.attributes.items())},
            "unmatched_sample": self.unmatched_sample,
            "problems": self.problems,
        }
