"""PDF annotation results (TOOL): `page` rules, bbox `text`, exporters and QA matching."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.exporters.base import split_image_items
from app.schemas.annotation import AnnotationResult
from app.services.agreement import match_shapes, match_spans, shape_category
from app.services.fusion import fuse


def _box(**extra: Any) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "type": "bbox",
        "class": "total",
        "bbox": [10, 20, 90, 40],
    } | extra


def _result(media_type: str, *shapes: dict[str, Any]) -> AnnotationResult:
    return AnnotationResult.model_validate(
        {"schema_version": 1, "media_type": media_type, "shapes": list(shapes)}
    )


def test_pdf_shapes_carry_a_page_and_optional_text() -> None:
    result = _result(
        "pdf",
        _box(page=2, text="Total 42,00 €"),
        {
            "id": str(uuid.uuid4()),
            "type": "polygon",
            "class": "stamp",
            "page": 1,
            "points": [[1, 1], [5, 1], [5, 5]],
        },
    )
    assert [shape.page for shape in result.shapes] == [2, 1]
    assert result.shapes[0].text == "Total 42,00 €"  # type: ignore[union-attr]  # bbox shape
    dumped = result.model_dump(mode="json", by_alias=True)
    assert dumped["shapes"][0]["page"] == 2


@pytest.mark.parametrize(
    ("media_type", "shape", "message"),
    [
        ("pdf", _box(), "'page' is required on pdf items"),
        ("pdf", _box(page=0), "greater than or equal to 1"),
        ("image", _box(page=1), "'page' is only allowed on pdf items"),
        ("video", _box(page=1, frame=0), "'page' is only allowed on pdf items"),
        (
            "pdf",
            {
                "id": str(uuid.uuid4()),
                "type": "mask",
                "class": "x",
                "page": 1,
                "rle": {"size": [2, 2], "counts": [4]},
            },
            "'mask' is not allowed on pdf items",
        ),
        (
            "pdf",
            {
                "id": str(uuid.uuid4()),
                "type": "span",
                "class": "x",
                "page": 1,
                "start": 0,
                "end": 1,
            },
            "a pdf span needs 'boxes'",
        ),
        (
            "text",
            {
                "id": str(uuid.uuid4()),
                "type": "span",
                "class": "x",
                "boxes": [[1, 1, 5, 5]],
            },
            "'boxes' is only allowed on pdf spans",
        ),
        (
            "image",
            {"id": str(uuid.uuid4()), "type": "span", "class": "x", "start": 0, "end": 1},
            "only allowed on text and pdf items",
        ),
        (
            "pdf",
            {
                "id": str(uuid.uuid4()),
                "type": "relation",
                "class": "r",
                "page": 1,
                "from": str(uuid.uuid4()),
                "to": str(uuid.uuid4()),
            },
            "a relation takes no 'page'",
        ),
    ],
)
def test_page_rules(media_type: str, shape: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _result(media_type, shape)


def test_image_only_exporters_skip_pdf_items() -> None:
    class Item:
        path = "docs/invoice.pdf"
        result = _result("pdf", _box(page=1))

    kept, warnings = split_image_items([Item()])  # type: ignore[list-item]  # duck-typed ExportItem
    assert kept == []
    assert warnings == ["docs/invoice.pdf: skipped pdf item, not supported by this format"]


def test_boxes_on_different_pages_never_match() -> None:
    on_one = _result("pdf", _box(page=1)).shapes
    on_two = _result("pdf", _box(page=2)).shapes
    assert shape_category(on_one[0]) is not None
    assert match_shapes(on_one, on_one).matched == 1
    assert match_shapes(on_one, on_two).matched == 0


def _pdf_span(**extra: Any) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "type": "span",
        "class": "PER",
        "page": 1,
        "boxes": [[412.0, 96.2, 470.4, 108.0], [72.0, 110.1, 118.6, 121.9]],
        "text": "Alice Smith",
    } | extra


def test_pdf_spans_and_relations_across_pages() -> None:
    span = _pdf_span()
    key = _box(page=2)
    result = _result(
        "pdf",
        span,
        key,
        {
            "id": str(uuid.uuid4()),
            "type": "relation",
            "class": "r",
            "from": span["id"],
            "to": key["id"],
        },
    )
    spans = [s for s in result.shapes if s.type == "span"]
    assert spans[0].boxes == [(412.0, 96.2, 470.4, 108.0), (72.0, 110.1, 118.6, 121.9)]
    assert spans[0].start is None


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"boxes": []}, "at least 1 item"),
        ({"boxes": [[10, 10, 5, 20]]}, "x_max must be greater"),
        ({"boxes": [[1, 1, 2, 2]] * 257}, "at most 256 items"),
        ({"start": 0, "end": 3}, "not both"),
        ({"boxes": None}, "needs 'start'/'end'"),
    ],
)
def test_pdf_span_anchor_rules(extra: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _result("pdf", _pdf_span(**extra))


def test_text_span_needs_both_offsets() -> None:
    span = {"id": str(uuid.uuid4()), "type": "span", "class": "PER", "start": 3}
    with pytest.raises(ValidationError, match="needs both 'start' and 'end'"):
        _result("text", span)


def test_pdf_spans_match_exactly_on_rounded_boxes_and_by_overlap() -> None:
    a = _result("pdf", _pdf_span()).shapes
    nudged = _result(
        "pdf",
        _pdf_span(boxes=[[72.2, 110.0, 118.7, 122.1], [411.9, 96.0, 470.3, 108.2]]),
    ).shapes
    shorter = _result("pdf", _pdf_span(boxes=[[412.0, 96.2, 440.0, 108.0]])).shapes
    other_page = _result("pdf", _pdf_span(page=2)).shapes
    other_class = _result("pdf", _pdf_span(**{"class": "ORG"})).shapes

    same = match_spans(a, nudged)
    assert (same.exact_matched, same.overlap_matched) == (1, 1)
    partial = match_spans(a, shorter)
    assert (partial.exact_matched, partial.overlap_matched) == (0, 1)
    for other in (other_page, other_class):
        assert match_spans(a, other).overlap_matched == 0


def test_relations_between_pdf_spans_match_through_their_ends() -> None:
    def annotated() -> list[Any]:
        alice = _pdf_span()
        contoso = _pdf_span(**{"class": "ORG", "boxes": [[72, 300, 130, 312]]})
        relation = {
            "id": str(uuid.uuid4()),
            "type": "relation",
            "class": "works_for",
            "from": alice["id"],
            "to": contoso["id"],
        }
        return _result("pdf", alice, contoso, relation).shapes

    assert match_shapes(annotated(), annotated()).matched == 1


def test_fusion_keeps_pdf_spans_with_enough_votes() -> None:
    votes = [_result("pdf", _pdf_span()) for _ in range(2)] + [_result("pdf")]
    fused, conflicts = fuse(votes)
    spans = [s for s in fused.shapes if s.type == "span"]
    assert len(spans) == 1
    assert (spans[0].page, spans[0].boxes, spans[0].text) == (
        1,
        [(412.0, 96.2, 470.4, 108.0), (72.0, 110.1, 118.6, 121.9)],
        "Alice Smith",
    )
    assert conflicts == []

    lone, conflicts = fuse([_result("pdf", _pdf_span()), _result("pdf"), _result("pdf")])
    assert lone.shapes == []
    assert conflicts == ["span.PER"]
