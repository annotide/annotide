from __future__ import annotations

import hashlib
from typing import Any

import pytest

from annotide_training.dataset import DatasetError, SplitConfig, assign_split, load_export
from tests.conftest import DIGEST, SNAPSHOT, export_zip, record


def test_platform_split_is_used_as_is() -> None:
    archive = export_zip({"train": [record("a", [])], "test": [record("b", [])]})
    dataset = load_export(archive, snapshot_id=SNAPSHOT, snapshot_digest=DIGEST)
    assert dataset.split_source == "snapshot"
    assert dataset.counts == {"train": 1, "val": 0, "test": 1}


def test_unsplit_export_is_split_with_the_platform_rule() -> None:
    items = [record(f"item-{n}", [("car", [0, 0, 1, 1])]) for n in range(50)]
    config = SplitConfig(train=0.6, val=0.2, test=0.2, seed=7)
    dataset = load_export(
        export_zip(flat=items), snapshot_id=SNAPSHOT, snapshot_digest=DIGEST, split=config
    )
    assert dataset.split_source == "local"
    assert sum(dataset.counts.values()) == 50
    assert dataset.classes == ["car"]
    for name, records in dataset.records.items():
        assert all(assign_split(r["item_id"], config) == name for r in records)


def test_assign_split_matches_backend_formula() -> None:
    # backend/app/services/datasets.py::assign_split, ungrouped: "{seed}:item:{id}".
    point = int.from_bytes(hashlib.sha256(b"3:item:abc").digest()[:8], "big") / 2**64
    expected = "train" if point < 0.8 else "val" if point < 0.9 else "test"
    assert assign_split("abc", SplitConfig(seed=3)) == expected


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"snapshot_id": "other"}, "not 222"),
        ({"digest": "b" * 64}, "digest mismatch"),
        ({"export_format": "coco"}, "native"),
    ],
)
def test_an_export_of_something_else_is_refused(kwargs: dict[str, Any], message: str) -> None:
    archive = export_zip({"train": [record("a", [])]}, **kwargs)
    with pytest.raises(DatasetError, match=message):
        load_export(archive, snapshot_id=SNAPSHOT, snapshot_digest=DIGEST)


def test_malformed_archives() -> None:
    with pytest.raises(DatasetError, match="zip"):
        load_export(b"nope", snapshot_id=SNAPSHOT, snapshot_digest=DIGEST)
    with pytest.raises(DatasetError, match=r"annotations\.jsonl"):
        load_export(export_zip(), snapshot_id=SNAPSHOT, snapshot_digest=DIGEST)


def test_bad_split_ratios() -> None:
    with pytest.raises(ValueError, match="sum to 1"):
        SplitConfig(train=0.5, val=0.1, test=0.1)
