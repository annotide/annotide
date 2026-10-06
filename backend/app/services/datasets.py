"""Selecting a dataset — items with their latest annotation — and freezing it.

Both the snapshot job (EXP-1/2) and the export job (EXP-5) start from the same
question: *which items, with which annotation version?* The answer is always
"the latest version per item", narrowed by a :class:`DatasetFilter`. A
snapshot then writes that answer down so it never changes; an export renders
it (or a snapshot's frozen answer) into a format a training pipeline reads.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ValidationFailedError
from app.exporters import EXPORTERS, ExportFile, ExportItem, get_exporter
from app.models import (
    Annotation,
    AnnotationKind,
    AnnotationSource,
    AnnotationStatus,
    Item,
    ItemStatus,
    LabelSchemaVersion,
    Project,
)
from app.schemas import AnnotationResult, BaseSchema, LabelSchemaDefinition
from app.schemas.pdf_text import PdfWord
from app.services.annotations import build_blob_document


class DatasetFilter(BaseSchema):
    """Which items a snapshot or export covers (EXP-2). Empty means every annotated item.

    Every field is a further restriction on the *latest* annotation version
    of each item; the field list is documented in CONTRACTS.md → snapshot.
    """

    item_status: list[ItemStatus] | None = None
    annotation_status: list[AnnotationStatus] | None = None
    path_prefix: str | None = None
    #: Keep items whose latest version has at least one shape of these classes.
    classes: list[str] | None = None
    #: Keep items whose latest version was authored by one of these users.
    annotator_ids: list[UUID] | None = None
    #: `human` / `model` — who produced the latest version.
    source: list[AnnotationSource] | None = None
    #: Latest version created at or after this instant.
    annotated_after: datetime | None = None
    #: Latest version created before this instant.
    annotated_before: datetime | None = None


SplitName = Literal["train", "val", "test"]
SPLIT_NAMES: tuple[SplitName, ...] = ("train", "val", "test")


class SplitConfig(BaseSchema):
    """Train / val / test partition of a snapshot (EXP-3).

    Assignment is a hash of `seed` and the item's group key, so it is
    deterministic — the same snapshot config on the same items gives the same
    split, and adding items later does not move the ones already placed —
    and group-aware for free: every item sharing a group key hashes to the
    same value and lands in the same split, so frames of one video or tiles
    of one slide never leak between train and test.

    `group_by` is `None` (each item on its own), `"folder"` (the item path's
    parent directory) or `"meta.<key>"` (a value in `item.meta`; items
    without it fall back to their own id).
    """

    train: float = Field(default=0.8, ge=0, le=1)
    val: float = Field(default=0.1, ge=0, le=1)
    test: float = Field(default=0.1, ge=0, le=1)
    seed: int = 0
    group_by: str | None = Field(default=None, pattern=r"^(folder|meta\.[A-Za-z0-9_.-]+)$")

    @model_validator(mode="after")
    def _ratios_sum_to_one(self) -> SplitConfig:
        if abs(self.train + self.val + self.test - 1.0) > 1e-6:
            raise ValueError("train + val + test must sum to 1")
        return self


def split_group_key(item: Item, config: SplitConfig) -> str:
    """The string an item is hashed on when assigning its split."""
    if config.group_by == "folder":
        folder, _, _ = item.path.rpartition("/")
        return f"folder:{folder}"
    if config.group_by and config.group_by.startswith("meta."):
        value = item.meta.get(config.group_by[len("meta.") :])
        if value is not None and value != "":
            return f"meta:{value}"
    return f"item:{item.id}"


def assign_split(item: Item, config: SplitConfig) -> SplitName:
    """Map an item to `train` / `val` / `test` from a hash of its group key.

    sha256 of `"{seed}:{group_key}"`, first 8 bytes as a fraction of 2**64,
    bucketed by the cumulative ratios. Uniform enough at dataset sizes, and
    reproducible anywhere without a random module's platform quirks.
    """
    digest = hashlib.sha256(f"{config.seed}:{split_group_key(item, config)}".encode()).digest()
    point = int.from_bytes(digest[:8], "big") / 2**64
    if point < config.train:
        return "train"
    if point < config.train + config.val:
        return "val"
    return "test"


def split_counts(splits: dict[UUID, SplitName]) -> dict[str, int]:
    counts: dict[str, int] = dict.fromkeys(SPLIT_NAMES, 0)
    for name in splits.values():
        counts[name] += 1
    return counts


def _shape_classes(result: dict[str, Any]) -> set[str]:
    shapes = result.get("shapes")
    if not isinstance(shapes, list):
        return set()
    return {str(shape["class"]) for shape in shapes if isinstance(shape, dict) and "class" in shape}


def _aware(stamp: datetime) -> datetime:
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class DatasetEntry:
    """One item together with the annotation version that represents it."""

    item: Item
    annotation: Annotation


def snapshot_blob_prefix(snapshot_id: UUID) -> str:
    """`snapshots/{snapshot_id}/` — see the blob layout in CONTRACTS.md."""
    return f"snapshots/{snapshot_id}/"


def require_export_format(export_format: str) -> None:
    """Reject an unknown export format with a 422 listing the registered ones.

    Routers call this instead of touching `app.exporters` themselves, so the
    exporter registry stays behind the service layer.
    """
    if export_format not in EXPORTERS:
        known = ", ".join(sorted(EXPORTERS))
        raise ValidationFailedError(
            f"Unknown export format '{export_format}'. Known formats: {known}."
        )


def export_blob_path(job_id: UUID, export_format: str) -> str:
    """`exports/{job_id}/{format}.zip` — see the blob layout in CONTRACTS.md."""
    return f"exports/{job_id}/{export_format}.zip"


async def select_dataset(
    session: AsyncSession, project_id: UUID, dataset_filter: DatasetFilter
) -> list[DatasetEntry]:
    """Every item in the project that has an annotation, paired with its latest version.

    Ordered by item path so two runs over unchanged data produce identical
    output — which is what makes a snapshot digest meaningful.
    """
    latest = (
        select(Annotation.item_id, func.max(Annotation.version).label("version"))
        .where(Annotation.kind == AnnotationKind.PRIMARY)
        .group_by(Annotation.item_id)
        .subquery()
    )
    stmt = (
        select(Item, Annotation)
        .join(latest, latest.c.item_id == Item.id)
        .join(
            Annotation,
            (Annotation.item_id == Item.id) & (Annotation.version == latest.c.version),
        )
        .where(Item.project_id == project_id)
        .order_by(Item.path, Item.id)
    )
    if dataset_filter.item_status:
        stmt = stmt.where(Item.status.in_(dataset_filter.item_status))
    if dataset_filter.annotation_status:
        stmt = stmt.where(Annotation.status.in_(dataset_filter.annotation_status))
    if dataset_filter.path_prefix:
        stmt = stmt.where(Item.path.startswith(dataset_filter.path_prefix))
    if dataset_filter.annotator_ids:
        stmt = stmt.where(Annotation.author_user_id.in_(dataset_filter.annotator_ids))
    if dataset_filter.source:
        stmt = stmt.where(Annotation.source.in_(dataset_filter.source))

    rows = await session.execute(stmt)
    entries = [DatasetEntry(item=item, annotation=annotation) for item, annotation in rows]

    # The class filter reads into the result JSON and the date bounds must
    # survive SQLite's naive timestamps, so both are applied in Python. The
    # rows above are already narrowed by project, status and path.
    wanted = set(dataset_filter.classes or ())
    after = dataset_filter.annotated_after
    before = dataset_filter.annotated_before
    if wanted:
        entries = [e for e in entries if _shape_classes(e.annotation.result) & wanted]
    if after is not None:
        entries = [e for e in entries if _aware(e.annotation.created_at) >= _aware(after)]
    if before is not None:
        entries = [e for e in entries if _aware(e.annotation.created_at) < _aware(before)]
    return entries


async def load_entries(session: AsyncSession, annotation_ids: list[UUID]) -> list[DatasetEntry]:
    """The entries a snapshot manifest names, in manifest order.

    An annotation that has since been deleted is skipped rather than failing
    the export: the snapshot's own `annotations.jsonl` still has its content.
    """
    if not annotation_ids:
        return []
    rows = await session.execute(
        select(Item, Annotation)
        .join(Annotation, Annotation.item_id == Item.id)
        .where(Annotation.id.in_(annotation_ids))
    )
    by_id = {
        annotation.id: DatasetEntry(item=item, annotation=annotation) for item, annotation in rows
    }
    return [by_id[annotation_id] for annotation_id in annotation_ids if annotation_id in by_id]


async def resolve_schema_version(
    session: AsyncSession, project: Project, label_schema_version_id: UUID | None = None
) -> LabelSchemaVersion:
    """The schema version to export against: the one asked for, else the project's latest."""
    if label_schema_version_id is not None:
        version = await session.get(LabelSchemaVersion, label_schema_version_id)
        if version is None:
            raise LookupError(f"label schema version {label_schema_version_id} does not exist")
        return version
    if project.label_schema_id is None:
        raise LookupError(f"project {project.id} has no label schema")
    latest = await session.scalar(
        select(LabelSchemaVersion)
        .where(LabelSchemaVersion.label_schema_id == project.label_schema_id)
        .order_by(LabelSchemaVersion.version.desc())
        .limit(1)
    )
    if latest is None:
        raise LookupError(f"project {project.id} has a label schema with no versions")
    return latest


# --------------------------------------------------------------------------- #
# Snapshots (EXP-1, EXP-2)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SnapshotFiles:
    """What a snapshot job writes: the frozen data, its manifest, and the digest."""

    manifest: dict[str, Any]
    annotations_jsonl: bytes
    digest: str


def build_snapshot_files(
    *,
    snapshot_id: UUID,
    project_id: UUID,
    name: str,
    dataset_filter: DatasetFilter,
    label_schema_version_id: UUID,
    entries: list[DatasetEntry],
    created_at: datetime | None = None,
    split: SplitConfig | None = None,
) -> SnapshotFiles:
    """Render the entries to `annotations.jsonl` and describe them in a manifest.

    The digest is sha256 over the JSONL bytes, so a training run can prove it
    trained on exactly this data (EXP-8) and a re-created snapshot with the
    same content gets the same digest. With `split`, every JSONL document
    and manifest entry carries its `split` (EXP-3), and the manifest sums
    them up under `split.counts`.
    """
    lines: list[str] = []
    manifest_entries: list[dict[str, Any]] = []
    splits: dict[UUID, SplitName] = {}
    for entry in entries:
        result = AnnotationResult.model_validate(entry.annotation.result)
        document = build_blob_document(item=entry.item, annotation=entry.annotation, result=result)
        document["annotation_id"] = str(entry.annotation.id)
        manifest_entry = {
            "item_id": str(entry.item.id),
            "annotation_id": str(entry.annotation.id),
            "version": entry.annotation.version,
            "path": entry.item.path,
        }
        if split is not None:
            name = assign_split(entry.item, split)
            splits[entry.annotation.id] = name
            document["split"] = name
            manifest_entry["split"] = name
        lines.append(json.dumps(document, sort_keys=True, separators=(",", ":")))
        manifest_entries.append(manifest_entry)

    jsonl = (("\n".join(lines) + "\n") if lines else "").encode("utf-8")
    digest = hashlib.sha256(jsonl).hexdigest()
    manifest = {
        "snapshot_id": str(snapshot_id),
        "project_id": str(project_id),
        "name": name,
        "created_at": (created_at or datetime.now(UTC)).isoformat(),
        "label_schema_version_id": str(label_schema_version_id),
        "filter": dataset_filter.model_dump(mode="json", exclude_none=True),
        "item_count": len(entries),
        "digest": digest,
        "files": {"annotations": "annotations.jsonl"},
        "entries": manifest_entries,
    }
    if split is not None:
        manifest["split"] = {
            "config": split.model_dump(mode="json"),
            "counts": split_counts(splits),
        }
    return SnapshotFiles(manifest=manifest, annotations_jsonl=jsonl, digest=digest)


def manifest_annotation_ids(manifest: dict[str, Any]) -> list[UUID]:
    """The annotation ids a manifest freezes, in order."""
    return [UUID(str(entry["annotation_id"])) for entry in manifest.get("entries", [])]


def manifest_splits(manifest: dict[str, Any]) -> dict[UUID, SplitName]:
    """Annotation id → split name from a manifest; empty when it has no split."""
    if "split" not in manifest:
        return {}
    splits: dict[UUID, SplitName] = {}
    for entry in manifest.get("entries", []):
        name = entry.get("split")
        if name in SPLIT_NAMES:
            splits[UUID(str(entry["annotation_id"]))] = name
    return splits


# --------------------------------------------------------------------------- #
# Exports (EXP-5)
# --------------------------------------------------------------------------- #


def to_export_items(
    entries: list[DatasetEntry],
    texts: dict[UUID, str] | None = None,
    pdf_words: dict[UUID, list[PdfWord]] | None = None,
) -> list[ExportItem]:
    """`entries` as `ExportItem`s. `texts` (item id -> source content) fills in

    `ExportItem.text` for `spacy`/`conll` (EXP-5), `pdf_words` the same for
    their `pdf` items; other formats leave both `None`.
    """
    return [
        ExportItem(
            id=entry.item.id,
            path=entry.item.path,
            width=entry.item.width,
            height=entry.item.height,
            result=AnnotationResult.model_validate(entry.annotation.result),
            text=texts.get(entry.item.id) if texts else None,
            pdf_words=pdf_words.get(entry.item.id) if pdf_words else None,
        )
        for entry in entries
    ]


def build_export_archive(
    *,
    export_format: str,
    entries: list[DatasetEntry],
    definition: LabelSchemaDefinition,
    manifest: dict[str, Any],
    splits: dict[UUID, SplitName] | None = None,
    texts: dict[UUID, str] | None = None,
    pdf_words: dict[UUID, list[PdfWord]] | None = None,
) -> bytes:
    """Run the exporter and zip its files together with a small manifest.

    The manifest records where the export came from (project, snapshot, filter,
    schema version) so a zip found on disk a year later still explains itself.
    With `splits` (annotation id → name, from a split snapshot) the exporter
    runs once per split and its files land under `train/`, `val/`, `test/`
    (EXP-3); entries missing from `splits` are left out. `texts` (item id ->
    source content) and `pdf_words` are only used by the text formats (EXP-5).
    """
    exporter = get_exporter(export_format)
    files: list[ExportFile] = []
    if splits is None:
        files.extend(
            exporter.export(to_export_items(entries, texts=texts, pdf_words=pdf_words), definition)
        )
    else:
        for name in SPLIT_NAMES:
            subset = [entry for entry in entries if splits.get(entry.annotation.id) == name]
            if not subset:
                continue
            for export_file in exporter.export(
                to_export_items(subset, texts=texts, pdf_words=pdf_words), definition
            ):
                files.append(ExportFile(path=f"{name}/{export_file.path}", data=export_file.data))
        manifest = {**manifest, "split_counts": split_counts(splits)}
    files.append(
        ExportFile(
            path="manifest.json",
            data=json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"),
        )
    )

    buffer = io.BytesIO()
    # Fixed timestamps: the archive bytes then depend only on the content.
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for export_file in files:
            info = zipfile.ZipInfo(export_file.path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, export_file.data)
    return buffer.getvalue()
