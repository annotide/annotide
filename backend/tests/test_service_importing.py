"""Tests for app.services.importing: matching, class mapping, coordinates, input reading."""

from __future__ import annotations

import asyncio
import io
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest

from app.connectors.local import LocalConnector
from app.demo import DEMO_SCHEMA
from app.importers import ImportedItem, ImportFormatError
from app.models import Item, MediaType
from app.schemas import LabelSchemaDefinition
from app.services import importing
from app.services.importing import ImportTally, ItemIndex, convert_item, read_import_files

DEFINITION = LabelSchemaDefinition.model_validate(DEMO_SCHEMA)


def _item(path: str, width: int | None = 640, height: int | None = 480) -> Item:
    return Item(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        connector_id=uuid.uuid4(),
        path=path,
        media_type=MediaType.IMAGE,
        size_bytes=1,
        width=width,
        height=height,
        meta={},
    )


class TestItemIndex:
    def test_exact_path_then_unique_name_then_unique_stem(self) -> None:
        a, b, c = _item("images/a.png"), _item("images/sub/b.jpg"), _item("images/c.png")
        index = ItemIndex.build([a, b, c])

        assert index.resolve("images/a.png") is a
        assert index.resolve("./images/a.png") is a
        assert index.resolve("b.jpg") is b
        assert index.resolve("elsewhere/b.jpg") is b
        assert index.resolve("c") is c  # YOLO label stem
        assert index.resolve("c.jpeg") is c  # different extension, unique stem
        assert index.resolve("missing.png") is None

    def test_ambiguous_names_and_stems_do_not_resolve(self) -> None:
        index = ItemIndex.build([_item("x/a.png"), _item("y/a.png"), _item("z/a.jpg")])

        assert index.resolve("a.png") is None
        assert index.resolve("a") is None
        assert index.resolve("x/a.png") is not None


class TestConvertItem:
    def test_maps_classes_drops_unknown_and_assigns_ids(self) -> None:
        imported = ImportedItem(
            path="a.png",
            shapes=[
                {"type": "bbox", "class": "automobile", "bbox": [1, 2, 3, 4]},
                {"type": "bbox", "class": "unicorn", "bbox": [1, 2, 3, 4]},
            ],
            classification={"weather": "rain"},
        )
        converted = convert_item(
            imported, _item("a.png"), DEFINITION, {"automobile": "car"}, schema_version=1
        )
        assert converted.result is not None
        assert converted.dropped_shapes == 1
        [shape] = converted.result.shapes
        assert shape.class_ == "car"
        assert isinstance(shape.id, uuid.UUID)
        assert converted.result.classification == {"weather": "rain"}

    def test_normalised_coordinates_use_the_item_dimensions(self) -> None:
        imported = ImportedItem(
            path="a",
            coordinates="normalized",
            shapes=[
                {"type": "bbox", "class": "car", "bbox": [0.25, 0.5, 0.5, 1.0]},
                {"type": "polygon", "class": "car", "points": [[0, 0], [0.5, 0], [0.5, 0.5]]},
                {"type": "point", "class": "car", "point": [0.1, 0.1]},
            ],
        )
        converted = convert_item(
            imported, _item("a.png", 200, 100), DEFINITION, {}, schema_version=1
        )
        assert converted.result is not None
        dumped = [s.model_dump(mode="json", by_alias=True) for s in converted.result.shapes]
        assert dumped[0]["bbox"] == [50.0, 50.0, 100.0, 100.0]
        assert dumped[1]["points"] == [[0.0, 0.0], [100.0, 0.0], [100.0, 50.0]]
        assert dumped[2]["point"] == [20.0, 10.0]

    def test_normalised_coordinates_fall_back_to_the_source_dimensions(self) -> None:
        imported = ImportedItem(
            path="a",
            width=200,
            height=100,
            coordinates="normalized",
            shapes=[{"type": "bbox", "class": "car", "bbox": [0.0, 0.0, 0.5, 0.5]}],
        )
        converted = convert_item(
            imported, _item("a.png", None, None), DEFINITION, {}, schema_version=1
        )
        assert converted.result is not None
        assert converted.result.shapes[0].model_dump()["bbox"] == (0.0, 0.0, 100.0, 50.0)

    def test_normalised_without_any_dimensions_is_a_problem(self) -> None:
        imported = ImportedItem(
            path="a",
            coordinates="normalized",
            shapes=[{"type": "bbox", "class": "car", "bbox": [0.0, 0.0, 0.5, 0.5]}],
        )
        converted = convert_item(
            imported, _item("a.png", None, None), DEFINITION, {}, schema_version=1
        )
        assert converted.result is None
        assert "dimensions are unknown" in converted.problems[0]

    def test_shape_validation_errors_are_problems_and_keep_importer_warnings(self) -> None:
        imported = ImportedItem(
            path="a.png",
            shapes=[{"type": "bbox", "class": "car", "bbox": [10, 10, 5, 20]}],
            warnings=["a.png: skipped something"],
        )
        converted = convert_item(imported, _item("a.png"), DEFINITION, {}, schema_version=1)
        assert converted.result is None
        assert converted.problems[0] == "a.png: skipped something"
        assert "x_max" in converted.problems[1]


class TestImportTally:
    def test_attribute_mapping_renames_discards_and_drops_undeclared(self) -> None:
        imported = ImportedItem(
            path="a.png",
            shapes=[
                {
                    "type": "bbox",
                    "class": "automobile",
                    "bbox": [1, 2, 3, 4],
                    "attributes": {"is_occluded": True, "track_id": 7, "colour": "red"},
                },
                {
                    "type": "bbox",
                    "class": "sign",
                    "bbox": [1, 2, 3, 4],
                    "attributes": {"is_occluded": False},
                },
            ],
        )
        converted = convert_item(
            imported,
            _item("a.png"),
            DEFINITION,
            {"automobile": "car"},
            schema_version=1,
            # Keyed by the *mapped* class; the class entry wins over "*".
            attribute_mapping={
                "*": {"is_occluded": None, "track_id": None},
                "car": {"is_occluded": "occluded"},
            },
        )
        assert converted.result is not None
        car, sign = converted.result.shapes
        assert car.attributes == {"occluded": True}
        assert sign.attributes == {}
        # car: track_id (discarded) + colour (undeclared); sign: is_occluded (discarded).
        assert converted.dropped_attributes == 3
        assert converted.problems == []

    def test_undeclared_attributes_are_dropped_not_failed_without_a_mapping(self) -> None:
        imported = ImportedItem(
            path="a.png",
            shapes=[
                {
                    "type": "bbox",
                    "class": "car",
                    "bbox": [1, 2, 3, 4],
                    "attributes": {"occluded": True, "truncated": True},
                }
            ],
        )
        converted = convert_item(imported, _item("a.png"), DEFINITION, {}, schema_version=1)
        assert converted.result is not None
        assert converted.result.shapes[0].attributes == {"occluded": True}
        assert converted.dropped_attributes == 1

    def test_tally_lists_source_attributes_per_source_class(self) -> None:
        tally = ImportTally()
        tally.note_shape({"class": "automobile", "attributes": {"b": 1, "a": 2}})
        tally.note_shape({"class": "automobile", "attributes": {"a": 3}})
        tally.note_shape({"class": "sign"})
        result = tally.as_result(import_format="coco", dry_run=True)
        assert result["classes"] == {"automobile": 2, "sign": 1}
        assert result["attributes"] == {"automobile": ["a", "b"]}
        assert result["dropped_attributes"] == 0

    def test_samples_are_capped_and_counts_are_not(self) -> None:
        tally = ImportTally()
        for n in range(importing.RESULT_SAMPLE_SIZE + 5):
            tally.note_unmatched(f"{n}.png")
        tally.note_problems([f"p{n}" for n in range(importing.RESULT_SAMPLE_SIZE + 5)])
        result = tally.as_result(import_format="coco", dry_run=True)
        assert result["unmatched"] == importing.RESULT_SAMPLE_SIZE + 5
        assert len(result["unmatched_sample"]) == importing.RESULT_SAMPLE_SIZE
        assert len(result["problems"]) == importing.RESULT_SAMPLE_SIZE


def _zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _read(root: Path, path: str) -> list[Any]:
    async def _go() -> list[Any]:
        storage = LocalConnector(root)
        try:
            return await read_import_files(storage, path)
        finally:
            await storage.aclose()

    return asyncio.run(_go())


class TestReadImportFiles:
    def test_single_file_archive_and_prefix(self, tmp_path: Path) -> None:
        (tmp_path / "one.json").write_bytes(b"{}")
        (tmp_path / "set.zip").write_bytes(
            _zip({"labels/a.txt": b"0 0.5 0.5 0.1 0.1", "__MACOSX/._a.txt": b"junk"})
        )
        (tmp_path / "dir" / "sub").mkdir(parents=True)
        (tmp_path / "dir" / "sub" / "b.xml").write_bytes(b"<annotation/>")

        [single] = _read(tmp_path, "one.json")
        assert (single.path, single.data) == ("one.json", b"{}")

        [entry] = _read(tmp_path, "set.zip")
        assert entry.path == "labels/a.txt"

        [nested] = _read(tmp_path, "dir/")
        assert nested.path == "sub/b.xml"

    def test_empty_inputs_and_bad_archives_are_format_errors(self, tmp_path: Path) -> None:
        (tmp_path / "bad.zip").write_bytes(b"nope")
        (tmp_path / "empty").mkdir()
        with pytest.raises(ImportFormatError):
            _read(tmp_path, "bad.zip")
        with pytest.raises(ImportFormatError):
            _read(tmp_path, "empty/")

    def test_size_limit_applies_before_and_after_unpacking(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(importing, "MAX_IMPORT_BYTES", 32)
        (tmp_path / "big.json").write_bytes(b"x" * 64)
        (tmp_path / "bomb.zip").write_bytes(_zip({"a.txt": b"x" * 64}))
        with pytest.raises(ImportFormatError, match="exceeds"):
            _read(tmp_path, "big.json")
        with pytest.raises(ImportFormatError, match="exceeds"):
            _read(tmp_path, "bomb.zip")
