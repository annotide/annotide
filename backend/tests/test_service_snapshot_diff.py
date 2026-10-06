"""`services/snapshot_diff.py` (EXP-4) over hand-built manifests and JSONL."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from app.services.snapshot_diff import diff_snapshots, parse_jsonl


@dataclass
class _Snap:
    """The columns `diff_snapshots` reads off a `Snapshot` row."""

    name: str
    id: UUID = field(default_factory=uuid4)
    digest: str = "d" * 64
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


def _shape(shape_id: UUID, cls: str, coords: tuple[int, ...] = (1, 2, 3, 4)) -> dict[str, Any]:
    return {"id": str(shape_id), "type": "bbox", "class": cls, "bbox": list(coords)}


def _doc(annotation_id: UUID, item_id: UUID, version: int, shapes: list[dict[str, Any]]) -> str:
    return json.dumps(
        {
            "annotation_id": str(annotation_id),
            "item_id": str(item_id),
            "version": version,
            "result": {
                "schema_version": 1,
                "media_type": "image",
                "classification": {},
                "shapes": shapes,
            },
        }
    )


def _manifest(entries: list[dict[str, Any]], split: dict[str, Any] | None = None) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "item_count": len(entries),
        "digest": "m" * 64,
        "entries": entries,
    }
    if split is not None:
        manifest["split"] = split
    return manifest


def _entry(
    item_id: UUID, annotation_id: UUID, version: int, path: str, **extra: Any
) -> dict[str, Any]:
    return {
        "item_id": str(item_id),
        "annotation_id": str(annotation_id),
        "version": version,
        "path": path,
        **extra,
    }


class TestDiffSnapshots:
    def test_classifies_items_and_counts_shape_and_class_changes(self) -> None:
        kept, changed, removed, added = uuid4(), uuid4(), uuid4(), uuid4()
        a_kept, a_changed_v1, a_changed_v2, a_removed, a_added = (uuid4() for _ in range(5))
        s_same, s_moved, s_gone, s_new = (uuid4() for _ in range(4))

        base_manifest = _manifest(
            [
                _entry(kept, a_kept, 1, "images/kept.png"),
                _entry(changed, a_changed_v1, 1, "images/changed.png"),
                _entry(removed, a_removed, 1, "images/removed.png"),
            ]
        )
        base_docs = parse_jsonl(
            "\n".join(
                [
                    _doc(a_kept, kept, 1, [_shape(s_same, "car")]),
                    _doc(
                        a_changed_v1,
                        changed,
                        1,
                        [_shape(s_same, "car"), _shape(s_moved, "car"), _shape(s_gone, "person")],
                    ),
                    _doc(a_removed, removed, 1, [_shape(uuid4(), "person")]),
                ]
            ).encode()
        )
        target_manifest = _manifest(
            [
                _entry(kept, a_kept, 1, "images/kept.png"),
                _entry(changed, a_changed_v2, 2, "images/changed.png"),
                _entry(added, a_added, 1, "images/added.png"),
            ]
        )
        target_docs = parse_jsonl(
            "\n".join(
                [
                    _doc(a_kept, kept, 1, [_shape(s_same, "car")]),
                    _doc(
                        a_changed_v2,
                        changed,
                        2,
                        [
                            _shape(s_same, "car"),
                            _shape(s_moved, "car", (5, 6, 9, 9)),
                            _shape(s_new, "car"),
                        ],
                    ),
                    _doc(a_added, added, 1, [_shape(uuid4(), "bike")]),
                ]
            ).encode()
        )

        diff = diff_snapshots(
            base=_Snap("v1"),
            base_manifest=base_manifest,
            base_documents=base_docs,
            target=_Snap("v2"),
            target_manifest=target_manifest,
            target_documents=target_docs,
        )

        assert diff.items.model_dump() == {
            "added": 1,
            "removed": 1,
            "changed": 1,
            "unchanged": 1,
            "split_moved": 0,
        }
        assert [e.path for e in diff.added] == ["images/added.png"]
        assert [e.path for e in diff.removed] == ["images/removed.png"]
        assert diff.changed[0].path == "images/changed.png"
        assert (diff.changed[0].from_version, diff.changed[0].to_version) == (1, 2)
        assert diff.changed[0].shapes == {"added": 1, "removed": 1, "changed": 1}
        # car: 1+2 → 1+3 (+1); person: 1+1 → 0 (-2); bike: 0 → 1 (+1). Sorted by |delta|, name.
        assert [(c.name, c.base, c.target, c.delta) for c in diff.classes] == [
            ("person", 2, 0, -2),
            ("bike", 0, 1, 1),
            ("car", 3, 4, 1),
        ]
        assert diff.base.name == "v1"
        assert diff.target.item_count == 3
        assert diff.truncated is False

    def test_counts_split_moves_and_reports_split_totals(self) -> None:
        item, annotation = uuid4(), uuid4()
        base_manifest = _manifest(
            [_entry(item, annotation, 1, "a.png", split="train")],
            split={"config": {}, "counts": {"train": 1, "val": 0, "test": 0}},
        )
        target_manifest = _manifest(
            [_entry(item, annotation, 1, "a.png", split="val")],
            split={"config": {}, "counts": {"train": 0, "val": 1, "test": 0}},
        )
        docs = parse_jsonl(_doc(annotation, item, 1, []).encode())

        diff = diff_snapshots(
            base=_Snap("a"),
            base_manifest=base_manifest,
            base_documents=docs,
            target=_Snap("b"),
            target_manifest=target_manifest,
            target_documents=docs,
        )

        assert diff.items.unchanged == 1
        assert diff.items.split_moved == 1
        assert diff.base.split_counts == {"train": 1, "val": 0, "test": 0}
        assert diff.target.split_counts == {"train": 0, "val": 1, "test": 0}

    def test_lists_are_capped_but_counts_stay_exact(self) -> None:
        entries = [_entry(uuid4(), uuid4(), 1, f"images/{n:04d}.png") for n in range(7)]
        diff = diff_snapshots(
            base=_Snap("a"),
            base_manifest=_manifest([]),
            base_documents={},
            target=_Snap("b"),
            target_manifest=_manifest(entries),
            target_documents={},
            limit=5,
        )
        assert diff.items.added == 7
        assert len(diff.added) == 5
        assert diff.truncated is True
        assert diff.base.split_counts is None

    def test_missing_documents_still_yield_a_version_change(self) -> None:
        item = uuid4()
        diff = diff_snapshots(
            base=_Snap("a"),
            base_manifest=_manifest([_entry(item, uuid4(), 1, "a.png")]),
            base_documents={},
            target=_Snap("b"),
            target_manifest=_manifest([_entry(item, uuid4(), 3, "a.png")]),
            target_documents={},
        )
        assert diff.items.changed == 1
        assert diff.changed[0].shapes == {"added": 0, "removed": 0, "changed": 0}
