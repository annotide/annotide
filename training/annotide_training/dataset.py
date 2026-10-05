"""Prepare and split: turn a `native` export archive into train / val / test records.

The archive (CONTRACTS.md, export) holds `manifest.json` — with the
`snapshot_id` and `digest` it was exported from — and `annotations.jsonl`,
or one `annotations.jsonl` per `train/`, `val/`, `test/` when the snapshot
was split on the platform (EXP-3). An unsplit snapshot is split here with
the platform's own rule, so the same seed gives the same partition.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from typing import Any, Literal

SplitName = Literal["train", "val", "test"]
SPLITS: tuple[SplitName, ...] = ("train", "val", "test")


class DatasetError(ValueError):
    """The export is not the data the retrain request named, or is malformed."""


@dataclass(frozen=True, slots=True)
class SplitConfig:
    train: float = 0.8
    val: float = 0.1
    test: float = 0.1
    seed: int = 0

    def __post_init__(self) -> None:
        ratios = (self.train, self.val, self.test)
        if any(r < 0 or r > 1 for r in ratios) or abs(sum(ratios) - 1) > 1e-9:
            raise ValueError("split ratios must be in [0, 1] and sum to 1")


def assign_split(item_id: str, config: SplitConfig) -> SplitName:
    """`backend/app/services/datasets.py::assign_split` for an ungrouped item."""
    digest = hashlib.sha256(f"{config.seed}:item:{item_id}".encode()).digest()
    point = int.from_bytes(digest[:8], "big") / 2**64
    if point < config.train:
        return "train"
    if point < config.train + config.val:
        return "val"
    return "test"


@dataclass(slots=True)
class Dataset:
    snapshot_id: str
    digest: str
    manifest: dict[str, Any]
    records: dict[SplitName, list[dict[str, Any]]] = field(
        default_factory=lambda: {name: [] for name in SPLITS}
    )
    #: Whether the partition came from the platform (EXP-3) or `SplitConfig` here.
    split_source: Literal["snapshot", "local"] = "snapshot"

    @property
    def counts(self) -> dict[str, int]:
        return {name: len(self.records[name]) for name in SPLITS}

    @property
    def classes(self) -> list[str]:
        seen = {
            str(shape.get("class"))
            for records in self.records.values()
            for record in records
            for shape in record.get("shapes", [])
        }
        return sorted(seen)


def _jsonl(data: bytes, name: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for number, line in enumerate(data.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{name}:{number}: not JSON") from exc
        if not isinstance(record, dict) or "item_id" not in record:
            raise DatasetError(f"{name}:{number}: not a native export record")
        records.append(record)
    return records


def load_export(
    archive: bytes,
    *,
    snapshot_id: str,
    snapshot_digest: str,
    split: SplitConfig | None = None,
) -> Dataset:
    """Read a native export archive and check it came from the requested snapshot.

    The digest the event carried must be the digest the export's manifest
    records: training on anything else would register lineage that is not
    true (the platform would 409 the registration too, but only after the
    training run was spent).
    """
    try:
        bundle = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile as exc:
        raise DatasetError("export is not a zip archive") from exc
    with bundle:
        names = set(bundle.namelist())
        if "manifest.json" not in names:
            raise DatasetError("export has no manifest.json")
        manifest: dict[str, Any] = json.loads(bundle.read("manifest.json"))
        if manifest.get("format") != "native":
            raise DatasetError(f"expected a native export, got {manifest.get('format')!r}")
        if str(manifest.get("snapshot_id")) != snapshot_id:
            raise DatasetError(
                f"export is of snapshot {manifest.get('snapshot_id')}, not {snapshot_id}"
            )
        if manifest.get("digest") != snapshot_digest:
            raise DatasetError(
                f"snapshot digest mismatch: export {manifest.get('digest')}, "
                f"request {snapshot_digest}"
            )

        dataset = Dataset(snapshot_id=snapshot_id, digest=snapshot_digest, manifest=manifest)
        split_files = {name: f"{name}/annotations.jsonl" for name in SPLITS}
        if any(path in names for path in split_files.values()):
            for name, path in split_files.items():
                if path in names:
                    dataset.records[name] = _jsonl(bundle.read(path), path)
            return dataset

        if "annotations.jsonl" not in names:
            raise DatasetError("export has no annotations.jsonl")
        config = split or SplitConfig()
        dataset.split_source = "local"
        for record in _jsonl(bundle.read("annotations.jsonl"), "annotations.jsonl"):
            dataset.records[assign_split(str(record["item_id"]), config)].append(record)
        return dataset
