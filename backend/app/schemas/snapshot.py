"""Request/response DTOs for snapshots (EXP-1, EXP-2)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from app.schemas.common import BaseSchema


class SnapshotCreate(BaseSchema):
    """Body for `POST /projects/{id}/snapshots`. Queues a snapshot job.

    `filter` is a dataset filter (`services/datasets.py::DatasetFilter`,
    documented in CONTRACTS.md → snapshot); empty freezes every annotated
    item at its latest version.
    """

    name: str = Field(min_length=1, max_length=255)
    filter: dict[str, Any] = Field(default_factory=dict)
    label_schema_version_id: UUID | None = None
    #: Optional train / val / test partition (`SplitConfig`, EXP-3).
    split: dict[str, Any] | None = None


class SnapshotRead(BaseSchema):
    """A frozen, immutable dataset (EXP-1) as returned by the API."""

    id: UUID
    project_id: UUID
    name: str
    filter: dict[str, Any]
    split: dict[str, Any] | None = None
    label_schema_version_id: UUID
    item_count: int
    blob_path: str
    digest: str
    created_by_id: UUID
    created_at: datetime


# --------------------------------------------------------------------------- #
# Snapshot diff (EXP-4)
# --------------------------------------------------------------------------- #


class SnapshotDiffSide(BaseSchema):
    """One of the two snapshots being compared."""

    id: UUID
    name: str
    item_count: int
    digest: str
    created_at: datetime
    split_counts: dict[str, int] | None = None


class SnapshotDiffEntry(BaseSchema):
    """An item present on only one side."""

    item_id: UUID
    path: str
    version: int
    split: str | None = None


class SnapshotDiffChanged(BaseSchema):
    """An item frozen at different annotation versions on the two sides."""

    item_id: UUID
    path: str
    from_version: int
    to_version: int
    #: Shape-level counts between the two versions: `added`, `removed`, `changed`.
    shapes: dict[str, int]


class SnapshotDiffClass(BaseSchema):
    """Shape count per class on each side; `delta` is target minus base."""

    name: str
    base: int
    target: int
    delta: int


class SnapshotDiffItems(BaseSchema):
    """Item-level totals; complete even when the lists below are truncated."""

    added: int
    removed: int
    changed: int
    unchanged: int
    #: Items in both snapshots whose train/val/test split differs (EXP-3).
    split_moved: int


class SnapshotDiff(BaseSchema):
    """`GET /projects/{id}/snapshots/{base}/diff/{target}` (EXP-4)."""

    base: SnapshotDiffSide
    target: SnapshotDiffSide
    items: SnapshotDiffItems
    added: list[SnapshotDiffEntry]
    removed: list[SnapshotDiffEntry]
    changed: list[SnapshotDiffChanged]
    classes: list[SnapshotDiffClass]
    #: True when a list was cut at the server's limit; the `items` counts are still exact.
    truncated: bool


# --------------------------------------------------------------------------- #
# Lineage (EXP-8)
# --------------------------------------------------------------------------- #


class SnapshotLineageSnapshot(BaseSchema):
    """The snapshot side of a lineage answer."""

    id: UUID
    name: str
    digest: str
    item_count: int
    created_at: datetime


class SnapshotLineageVersion(BaseSchema):
    """A model version trained on the snapshot, with its downstream footprint."""

    id: UUID
    model_id: UUID
    model_name: str
    version: int
    snapshot_digest: str | None
    training_run: dict[str, Any] | None
    created_at: datetime
    #: Distinct items of this project the version wrote a draft for.
    items_predicted: int


class SnapshotLineage(BaseSchema):
    """`GET /projects/{id}/snapshots/{sid}/lineage` (EXP-8)."""

    snapshot: SnapshotLineageSnapshot
    versions: list[SnapshotLineageVersion]
