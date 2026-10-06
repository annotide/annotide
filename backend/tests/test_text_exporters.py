"""Tests for the spaCy / CoNLL text export formats (EXP-5).

CONTRACTS.md *Annotation result JSON* -> "Text export formats (EXP-5)":
both formats share one **entity set** (spans kept longest-first, ties by
earlier start, no overlaps; drops counted in warnings) and skip non-text
items into the same `warnings.json` as COCO/YOLO.
"""

from __future__ import annotations

import json
import uuid
from uuid import UUID

from app.exporters import ConllExporter, ExportItem, SpacyExporter
from app.exporters.base import build_entity_set
from app.schemas.annotation import AnnotationResult, SpanShape
from app.schemas.label_schema import LabelSchemaDefinition

_DEFINITION = LabelSchemaDefinition(version=1, classes=[])


def _span(
    start: int, end: int, cls: str = "PER", shape_id: UUID | None = None
) -> dict[str, object]:
    return {
        "id": str(shape_id or uuid.uuid4()),
        "type": "span",
        "start": start,
        "end": end,
        "class": cls,
    }


def _relation(from_id: UUID, to_id: UUID, cls: str = "rel") -> dict[str, object]:
    return {
        "id": str(uuid.uuid4()),
        "type": "relation",
        "from": str(from_id),
        "to": str(to_id),
        "class": cls,
    }


def _text_result(
    *shapes: dict[str, object], classification: dict[str, object] | None = None
) -> AnnotationResult:
    return AnnotationResult.model_validate(
        {
            "schema_version": 1,
            "media_type": "text",
            "classification": classification or {},
            "shapes": list(shapes),
        }
    )


def _text_item(
    text: str | None,
    *shapes: dict[str, object],
    item_id: UUID | None = None,
    path: str = "docs/a.txt",
) -> ExportItem:
    return ExportItem(
        id=item_id or uuid.uuid4(),
        path=path,
        width=None,
        height=None,
        result=_text_result(*shapes),
        text=text,
    )


def _image_item() -> ExportItem:
    result = AnnotationResult.model_validate(
        {"schema_version": 1, "media_type": "image", "shapes": []}
    )
    return ExportItem(
        id=uuid.uuid4(), path="images/a.png", width=10, height=10, result=result, text=None
    )


# --------------------------------------------------------------------------- #
# build_entity_set
# --------------------------------------------------------------------------- #


def test_build_entity_set_keeps_longest_first_and_drops_overlaps() -> None:
    short = SpanShape.model_validate(_span(0, 3, "A"))
    long = SpanShape.model_validate(_span(0, 5, "B"))
    disjoint = SpanShape.model_validate(_span(6, 8, "C"))

    kept, dropped = build_entity_set([short, long, disjoint])

    assert [(s.start, s.end) for s in kept] == [(0, 5), (6, 8)]
    assert dropped == 1


def test_build_entity_set_tie_break_by_earlier_start() -> None:
    first = SpanShape.model_validate(_span(0, 3, "A"))
    second = SpanShape.model_validate(_span(1, 4, "B"))

    kept, dropped = build_entity_set([second, first])

    assert [(s.start, s.end) for s in kept] == [(0, 3)]
    assert dropped == 1


# --------------------------------------------------------------------------- #
# spaCy
# --------------------------------------------------------------------------- #


class TestSpacyExporter:
    def test_exports_entities_spans_and_relations(self) -> None:
        per_id = uuid.uuid4()
        loc_id = uuid.uuid4()
        item = _text_item(
            "Anna went to Paris",
            _span(0, 4, "PER", per_id),
            _span(14, 19, "LOC", loc_id),
            _relation(per_id, loc_id, "visited"),
            item_id=uuid.uuid4(),
        )

        files = list(SpacyExporter().export([item], _DEFINITION))
        assert [f.path for f in files] == ["annotations.jsonl"]
        record = json.loads(files[0].data.decode("utf-8").strip())

        assert record["text"] == "Anna went to Paris"
        assert record["entities"] == [[0, 4, "PER"], [14, 19, "LOC"]]
        assert record["spans"]["sc"] == [[0, 4, "PER"], [14, 19, "LOC"]]
        assert record["relations"] == [[0, 1, "visited"]]
        assert record["classification"] == {}
        assert record["meta"] == {"item_id": str(item.id), "path": item.path}

    def test_spans_sc_keeps_overlaps_entities_drops_them(self) -> None:
        item = _text_item("abcdef", _span(0, 3, "A"), _span(1, 5, "B"))

        files = list(SpacyExporter().export([item], _DEFINITION))
        record = json.loads(files[0].data.decode("utf-8").strip())

        assert record["spans"]["sc"] == [[0, 3, "A"], [1, 5, "B"]]
        assert record["entities"] == [[1, 5, "B"]]
        warnings = json.loads(next(f.data for f in files if f.path == "warnings.json"))
        assert warnings == ["docs/a.txt: 1 span(s) dropped for overlapping"]

    def test_skips_non_text_items_with_a_warning(self) -> None:
        files = list(SpacyExporter().export([_image_item()], _DEFINITION))
        assert files[0].data == b""
        warnings = json.loads(next(f.data for f in files if f.path == "warnings.json"))
        assert warnings == ["images/a.png: skipped image item, not supported by this format"]

    def test_skips_text_item_with_unreadable_source(self) -> None:
        item = _text_item(None, _span(0, 1, "A"), path="docs/big.txt")

        files = list(SpacyExporter().export([item], _DEFINITION))

        assert files[0].data == b""
        warnings = json.loads(next(f.data for f in files if f.path == "warnings.json"))
        assert warnings == ["docs/big.txt: skipped, source text could not be read"]

    def test_no_files_beyond_annotations_when_nothing_to_warn_about(self) -> None:
        item = _text_item("hello", _span(0, 5, "GREETING"))
        files = list(SpacyExporter().export([item], _DEFINITION))
        assert [f.path for f in files] == ["annotations.jsonl"]


# --------------------------------------------------------------------------- #
# CoNLL
# --------------------------------------------------------------------------- #


class TestConllExporter:
    def test_tokenises_and_tags_iob2(self) -> None:
        item_id = uuid.uuid4()
        item = _text_item(
            "Anna, Paris.",
            _span(0, 4, "PER"),
            _span(6, 11, "LOC"),
            item_id=item_id,
            path="docs/a.txt",
        )

        files = list(ConllExporter().export([item], _DEFINITION))
        text = files[0].data.decode("utf-8")

        lines = text.strip("\n").split("\n")
        assert lines[0] == f"# item_id = {item_id}"
        assert lines[1] == "# path = docs/a.txt"
        assert lines[2:] == [
            "Anna\tB-PER",
            ",\tO",
            "Paris\tB-LOC",
            ".\tO",
        ]

    def test_blank_line_separates_items(self) -> None:
        item_a = _text_item("hi", _span(0, 2, "X"), path="a.txt")
        item_b = _text_item("bye", path="b.txt")

        files = list(ConllExporter().export([item_a, item_b], _DEFINITION))
        text = files[0].data.decode("utf-8")

        blocks = text.strip("\n").split("\n\n")
        assert len(blocks) == 2
        assert blocks[0].splitlines()[-1] == "hi\tB-X"
        assert blocks[1].splitlines()[-1] == "bye\tO"

    def test_token_cut_by_boundary_takes_tag_from_first_character(self) -> None:
        # The span [0, 4) covers "Anna" but the token "Anna," is a comma stuck
        # to a word only when the regex splits it into two tokens anyway; here
        # a span ending mid-token ("Ann" only) still tags the whole token "Anna"
        # as its first character (position 0) sits inside the span.
        item = _text_item("Anna", _span(0, 3, "PER"))

        files = list(ConllExporter().export([item], _DEFINITION))
        lines = files[0].data.decode("utf-8").strip("\n").split("\n")

        assert lines[-1] == "Anna\tB-PER"

    def test_skips_non_text_items_with_a_warning(self) -> None:
        files = list(ConllExporter().export([_image_item()], _DEFINITION))
        assert files[0].data == b""
        warnings = json.loads(next(f.data for f in files if f.path == "warnings.json"))
        assert warnings == ["images/a.png: skipped image item, not supported by this format"]
