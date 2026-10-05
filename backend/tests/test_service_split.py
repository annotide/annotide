"""Train / val / test split assignment (EXP-3) — `services/datasets.py`."""

from __future__ import annotations

import io
import json
import zipfile
from collections import Counter
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.demo import DEMO_SCHEMA
from app.models import Annotation, AnnotationSource, AnnotationStatus, Item, MediaType
from app.schemas import LabelSchemaDefinition
from app.services.datasets import (
    DatasetEntry,
    DatasetFilter,
    SplitConfig,
    assign_split,
    build_export_archive,
    build_snapshot_files,
    manifest_splits,
    split_group_key,
)


def _item(path: str, meta: dict[str, object] | None = None) -> Item:
    return Item(
        id=uuid4(),
        project_id=uuid4(),
        connector_id=uuid4(),
        path=path,
        media_type=MediaType.IMAGE,
        size_bytes=1,
        width=10,
        height=10,
        meta=meta or {},
    )


def _entry(item: Item) -> DatasetEntry:
    annotation = Annotation(
        id=uuid4(),
        item_id=item.id,
        version=1,
        label_schema_version_id=uuid4(),
        author_user_id=uuid4(),
        status=AnnotationStatus.SUBMITTED,
        source=AnnotationSource.HUMAN,
        result={
            "schema_version": 1,
            "media_type": "image",
            "classification": {},
            "shapes": [{"id": str(uuid4()), "type": "bbox", "class": "car", "bbox": [1, 2, 3, 4]}],
        },
    )
    return DatasetEntry(item=item, annotation=annotation)


class TestSplitConfig:
    def test_defaults_are_80_10_10(self) -> None:
        config = SplitConfig()
        assert (config.train, config.val, config.test) == (0.8, 0.1, 0.1)

    def test_ratios_must_sum_to_one(self) -> None:
        with pytest.raises(ValidationError, match="sum to 1"):
            SplitConfig(train=0.5, val=0.1, test=0.1)

    def test_group_by_accepts_folder_and_meta_keys_only(self) -> None:
        SplitConfig(group_by="folder")
        SplitConfig(group_by="meta.patient_id")
        with pytest.raises(ValidationError):
            SplitConfig(group_by="path")


class TestAssignSplit:
    def test_is_deterministic_for_the_same_seed_and_item(self) -> None:
        item = _item("images/a.png")
        config = SplitConfig(seed=7)
        assert assign_split(item, config) == assign_split(item, config)

    def test_roughly_follows_the_ratios(self) -> None:
        config = SplitConfig(train=0.7, val=0.2, test=0.1, seed=1)
        counts = Counter(assign_split(_item(f"images/{i}.png"), config) for i in range(2000))
        assert 0.65 < counts["train"] / 2000 < 0.75
        assert 0.15 < counts["val"] / 2000 < 0.25
        assert 0.05 < counts["test"] / 2000 < 0.15

    def test_seed_changes_the_assignment(self) -> None:
        items = [_item(f"images/{i}.png") for i in range(200)]
        a = [assign_split(item, SplitConfig(seed=1)) for item in items]
        b = [assign_split(item, SplitConfig(seed=2)) for item in items]
        assert a != b

    def test_folder_groups_keep_a_directory_together(self) -> None:
        config = SplitConfig(train=0.5, val=0.25, test=0.25, group_by="folder")
        for folder in range(30):
            names = {
                assign_split(_item(f"videos/{folder}/frame_{n}.png"), config) for n in range(10)
            }
            assert len(names) == 1

    def test_meta_groups_keep_a_key_together_and_fall_back_to_the_item(self) -> None:
        config = SplitConfig(train=0.5, val=0.25, test=0.25, group_by="meta.patient")
        grouped = {
            assign_split(_item(f"scans/{n}.png", {"patient": "p-1"}), config) for n in range(10)
        }
        assert len(grouped) == 1
        loose = _item("scans/x.png")
        assert split_group_key(loose, config) == f"item:{loose.id}"

    def test_all_zero_but_one_ratio_puts_everything_there(self) -> None:
        config = SplitConfig(train=0, val=0, test=1)
        assert {assign_split(_item(f"i/{n}.png"), config) for n in range(50)} == {"test"}


class TestSnapshotAndExportWithSplit:
    def test_manifest_and_jsonl_carry_the_split(self) -> None:
        entries = [_entry(_item(f"images/{n}.png")) for n in range(40)]
        config = SplitConfig(seed=3)

        files = build_snapshot_files(
            snapshot_id=uuid4(),
            project_id=uuid4(),
            name="s",
            dataset_filter=DatasetFilter(),
            label_schema_version_id=uuid4(),
            entries=entries,
            split=config,
        )

        manifest = files.manifest
        assert manifest["split"]["config"] == config.model_dump(mode="json")
        assert sum(manifest["split"]["counts"].values()) == 40
        assert all(entry["split"] in ("train", "val", "test") for entry in manifest["entries"])
        lines = [json.loads(line) for line in files.annotations_jsonl.decode().splitlines()]
        assert [line["split"] for line in lines] == [e["split"] for e in manifest["entries"]]
        splits = manifest_splits(manifest)
        assert len(splits) == 40
        assert splits[entries[0].annotation.id] == manifest["entries"][0]["split"]

    def test_no_split_leaves_manifest_and_documents_alone(self) -> None:
        files = build_snapshot_files(
            snapshot_id=uuid4(),
            project_id=uuid4(),
            name="s",
            dataset_filter=DatasetFilter(),
            label_schema_version_id=uuid4(),
            entries=[_entry(_item("images/a.png"))],
        )
        assert "split" not in files.manifest
        assert "split" not in files.manifest["entries"][0]
        assert "split" not in json.loads(files.annotations_jsonl.decode())
        assert manifest_splits(files.manifest) == {}

    def test_export_archive_puts_each_split_in_its_own_directory(self) -> None:
        entries = [_entry(_item(f"images/{n}.png")) for n in range(3)]
        splits = {
            entries[0].annotation.id: "train",
            entries[1].annotation.id: "val",
            entries[2].annotation.id: "train",
        }
        definition = LabelSchemaDefinition.model_validate(DEMO_SCHEMA)

        archive = build_export_archive(
            export_format="native",
            entries=entries,
            definition=definition,
            manifest={"job_id": "j"},
            splits=splits,  # type: ignore[arg-type]  # literal names in a plain dict
        )

        names = zipfile.ZipFile(io.BytesIO(archive)).namelist()
        assert "train/annotations.jsonl" in names
        assert "val/annotations.jsonl" in names
        assert not any(name.startswith("test/") for name in names)
        manifest = json.loads(zipfile.ZipFile(io.BytesIO(archive)).read("manifest.json"))
        assert manifest["split_counts"] == {"train": 2, "val": 1, "test": 0}
        train = zipfile.ZipFile(io.BytesIO(archive)).read("train/annotations.jsonl").decode()
        assert len(train.splitlines()) == 2
