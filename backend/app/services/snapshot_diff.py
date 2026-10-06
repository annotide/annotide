"""Compare two snapshots of the same project (EXP-4).

Pure functions over what a snapshot job wrote: the manifest (which item is
frozen at which annotation version) and `annotations.jsonl` (the documents
themselves). The router reads the blobs and hands them here, so the
comparison is testable without storage and the API never has to re-derive a
frozen set from the live database.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from typing import Any

from app.schemas import AnnotationResult
from app.schemas.snapshot import (
    SnapshotDiff,
    SnapshotDiffChanged,
    SnapshotDiffClass,
    SnapshotDiffEntry,
    SnapshotDiffItems,
    SnapshotDiffSide,
)
from app.services.annotations import diff_shapes

#: Per-list cap on the entries returned; the counts are always complete.
DIFF_LIST_LIMIT = 500


def parse_jsonl(data: bytes) -> dict[str, dict[str, Any]]:
    """`annotations.jsonl` → documents by `annotation_id`."""
    documents: dict[str, dict[str, Any]] = {}
    for line in data.decode("utf-8").splitlines():
        if not line.strip():
            continue
        document = json.loads(line)
        documents[str(document["annotation_id"])] = document
    return documents


def _class_counts(documents: Iterable[dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for document in documents:
        shapes = (document.get("result") or {}).get("shapes") or []
        for shape in shapes:
            if isinstance(shape, dict) and "class" in shape:
                counts[str(shape["class"])] += 1
    return counts


def _side(snapshot: Any, manifest: dict[str, Any]) -> SnapshotDiffSide:
    split = manifest.get("split")
    return SnapshotDiffSide(
        id=snapshot.id,
        name=snapshot.name,
        item_count=int(manifest.get("item_count", len(manifest.get("entries", [])))),
        digest=str(manifest.get("digest", snapshot.digest)),
        created_at=snapshot.created_at,
        split_counts=dict(split["counts"]) if isinstance(split, dict) else None,
    )


def diff_snapshots(
    *,
    base: Any,
    base_manifest: dict[str, Any],
    base_documents: dict[str, dict[str, Any]],
    target: Any,
    target_manifest: dict[str, Any],
    target_documents: dict[str, dict[str, Any]],
    limit: int = DIFF_LIST_LIMIT,
) -> SnapshotDiff:
    """What changed from `base` to `target`.

    Items are matched by `item_id`. An item in both manifests at the same
    annotation version is unchanged; at different versions it is *changed*,
    and its two documents are compared shape by shape (`diff_shapes`, so the
    same rules as the review diff apply). Class balance is counted over every
    shape in each snapshot's documents, and `delta` is target minus base.
    """
    base_entries = {str(e["item_id"]): e for e in base_manifest.get("entries", [])}
    target_entries = {str(e["item_id"]): e for e in target_manifest.get("entries", [])}

    added = [
        SnapshotDiffEntry(
            item_id=e["item_id"],
            path=str(e["path"]),
            version=int(e["version"]),
            split=e.get("split"),
        )
        for item_id, e in target_entries.items()
        if item_id not in base_entries
    ]
    removed = [
        SnapshotDiffEntry(
            item_id=e["item_id"],
            path=str(e["path"]),
            version=int(e["version"]),
            split=e.get("split"),
        )
        for item_id, e in base_entries.items()
        if item_id not in target_entries
    ]

    changed: list[SnapshotDiffChanged] = []
    unchanged = 0
    split_moved = 0
    for item_id, before in base_entries.items():
        after = target_entries.get(item_id)
        if after is None:
            continue
        if before.get("split") != after.get("split") and "split" in before and "split" in after:
            split_moved += 1
        if str(before["annotation_id"]) == str(after["annotation_id"]):
            unchanged += 1
            continue
        shapes = {"added": 0, "removed": 0, "changed": 0}
        before_doc = base_documents.get(str(before["annotation_id"]))
        after_doc = target_documents.get(str(after["annotation_id"]))
        if before_doc is not None and after_doc is not None:
            shape_diff = diff_shapes(
                AnnotationResult.model_validate(before_doc["result"]),
                AnnotationResult.model_validate(after_doc["result"]),
            )
            shapes = {key: len(ids) for key, ids in shape_diff.items()}
        changed.append(
            SnapshotDiffChanged(
                item_id=after["item_id"],
                path=str(after["path"]),
                from_version=int(before["version"]),
                to_version=int(after["version"]),
                shapes=shapes,
            )
        )

    base_classes = _class_counts(base_documents.values())
    target_classes = _class_counts(target_documents.values())
    classes = sorted(
        (
            SnapshotDiffClass(
                name=name,
                base=base_classes.get(name, 0),
                target=target_classes.get(name, 0),
                delta=target_classes.get(name, 0) - base_classes.get(name, 0),
            )
            for name in set(base_classes) | set(target_classes)
        ),
        key=lambda row: (-abs(row.delta), row.name),
    )

    added.sort(key=lambda e: e.path)
    removed.sort(key=lambda e: e.path)
    changed.sort(key=lambda e: e.path)
    truncated = max(len(added), len(removed), len(changed)) > limit
    return SnapshotDiff(
        base=_side(base, base_manifest),
        target=_side(target, target_manifest),
        items=SnapshotDiffItems(
            added=len(added),
            removed=len(removed),
            changed=len(changed),
            unchanged=unchanged,
            split_moved=split_moved,
        ),
        added=added[:limit],
        removed=removed[:limit],
        changed=changed[:limit],
        classes=classes,
        truncated=truncated,
    )
