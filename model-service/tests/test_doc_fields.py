"""PDF words and document-field rules (reference pre-labelling for pdf items)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.backends.doc_fields import (
    Word,
    find_fields,
    line_boxes,
    page_text,
    pdf_words,
    words_in_range,
)
from app.main import app
from app.schemas import AnnotationResult
from tests.pdfs import pdf


def _word(text: str, x: float, page: int = 1) -> Word:
    return Word(page=page, text=text, bbox=(x, 90.0, x + 10 * len(text), 102.0))


class TestPdfWords:
    def test_words_are_in_top_left_page_points(self) -> None:
        words = pdf_words(pdf(["Total 42,00 EUR"]))
        assert [w.text for w in words] == ["Total", "42,00", "EUR"]
        x0, y0, x1, y1 = words[0].bbox
        # Baseline at y = 700 (PDF space) is 100 pt from the top; 12 pt glyphs sit above it.
        assert 99 <= x0 <= 101
        assert 85 <= y0 < y1 <= 103
        assert words[1].bbox[0] > x1

    def test_rotated_pages_use_the_rotated_space(self) -> None:
        [word] = pdf_words(pdf(["", "Hello"], rotate={1: 90}))
        assert word.page == 2
        x0, y0, x1, y1 = word.bbox
        # Rotated 90° clockwise the line runs down the 800 x 600 view.
        assert y1 - y0 > x1 - x0
        assert 690 <= x1 <= 715  # baseline 700 in PDF space is now x ≈ 700
        assert 99 <= y0 <= 101

    def test_the_crop_box_is_the_page_origin(self) -> None:
        [plain] = pdf_words(pdf(["Hello"]))
        [cropped] = pdf_words(pdf(["Hello"], crop=(50, 0, 550, 750)))
        shifted = tuple(c - 50 for c in plain.bbox)
        assert cropped.bbox == pytest.approx(shifted)

    def test_a_scan_has_no_words(self) -> None:
        assert pdf_words(pdf([""])) == []


class TestFindFields:
    def test_amount_merges_an_adjacent_currency_word(self) -> None:
        words = [_word("Total", 100), _word("42,00", 160), _word("€", 220)]
        [field] = find_fields(words)
        assert (field.label, field.text) == ("amount", "42,00 €")
        assert field.bbox == (160, 90.0, 230, 102.0)

    def test_leading_currency_and_thousands(self) -> None:
        [field] = find_fields([_word("EUR", 100), _word("1.234,50", 140)])
        assert field.text == "EUR 1.234,50"

    @pytest.mark.parametrize(
        ("text", "label"),
        [("28.09.2026", "date"), ("2026-09-28", "date"), ("anna@example.com", "email")],
    )
    def test_dates_and_emails(self, text: str, label: str) -> None:
        [field] = find_fields([_word(f"({text}),", 10)])
        assert (field.label, field.text) == (label, text)

    def test_plain_words_and_numbers_are_not_fields(self) -> None:
        assert find_fields([_word("Invoice", 0), _word("2026", 80), _word("42", 140)]) == []

    def test_a_currency_on_another_page_is_not_merged(self) -> None:
        [field] = find_fields([_word("42,00", 10), _word("€", 80, page=2)])
        assert field.text == "42,00"


def _at(text: str, x: float, y: float, *, nl: bool = False) -> Word:
    return Word(page=1, text=text, bbox=(x, y, x + 10 * len(text), y + 12), newline_before=nl)


class TestLines:
    def test_pdf_words_flag_line_breaks(self) -> None:
        words = pdf_words(pdf(["Anna Virtanen\nHelsinki"]))
        assert [(w.text, w.newline_before) for w in words] == [
            ("Anna", False),
            ("Virtanen", False),
            ("Helsinki", True),
        ]
        assert words[2].bbox[1] > words[0].bbox[3] - 1  # lower on the page

    def test_page_text_joins_with_space_or_newline(self) -> None:
        words = [_at("Anna", 0, 0), _at("Virtanen", 50, 0), _at("Oy", 0, 20, nl=True)]
        text, ranges = page_text(words)
        assert text == "Anna Virtanen\nOy"
        assert ranges == [(0, 4), (5, 13), (14, 16)]
        assert words_in_range(ranges, 3, 6) == [0, 1]
        assert words_in_range(ranges, 13, 14) == []

    def test_one_box_per_line(self) -> None:
        words = [_at("Anna", 0, 0), _at("Virtanen", 50, 1), _at("Oy", 0, 20, nl=True)]
        assert line_boxes(words) == [(0, 0, 130, 13), (0, 20, 20, 32)]

    def test_same_line_is_one_box(self) -> None:
        assert line_boxes([_at("A", 0, 0), _at("B", 20, 3)]) == [(0, 0, 30, 15)]


SCHEMA: dict[str, Any] = {
    "version": 1,
    "classes": [
        {"name": "amount", "display_name": "Amount", "color": "#e11d48", "tools": ["bbox"]},
        {"name": "date", "display_name": "Date", "color": "#2563eb", "tools": ["bbox"]},
    ],
    "classification": [],
}


class TestPredictPdf:
    def test_boxes_carry_page_and_text(self, httpserver: Any) -> None:
        httpserver.expect_request("/doc.pdf").respond_with_data(
            pdf(["Cover", "Due 28.09.2026 total 42,00 EUR"]), content_type="application/pdf"
        )
        with TestClient(app) as client:
            assert "pdf" in client.get("/info").json()["media_types"]
            response = client.post(
                "/predict",
                json={
                    "items": [
                        {
                            "id": "doc",
                            "url": httpserver.url_for("/doc.pdf"),
                            "width": 0,
                            "height": 0,
                            "media_type": "pdf",
                        }
                    ],
                    "schema": SCHEMA,
                },
            )
        assert response.status_code == 200, response.text
        [prediction] = response.json()["predictions"]
        result = AnnotationResult.model_validate(prediction["result"])
        assert result.media_type == "pdf"
        found = {(s.class_, s.page, s.text) for s in result.shapes}  # type: ignore[union-attr]
        assert found == {("date", 2, "28.09.2026"), ("amount", 2, "42,00 EUR")}

    def test_an_unreadable_pdf_is_an_item_error(self, httpserver: Any) -> None:
        httpserver.expect_request("/bad.pdf").respond_with_data(b"nope")
        with TestClient(app) as client:
            body = client.post(
                "/predict",
                json={
                    "items": [
                        {
                            "id": "bad",
                            "url": httpserver.url_for("/bad.pdf"),
                            "width": 0,
                            "height": 0,
                            "media_type": "pdf",
                        }
                    ],
                    "schema": SCHEMA,
                },
            ).json()
        [prediction] = body["predictions"]
        assert prediction["error"].startswith("could not read pdf")


SPAN_SCHEMA: dict[str, Any] = {
    "version": 1,
    "classes": [
        {"name": "PER", "display_name": "Person", "color": "#e11d48", "tools": ["span"]},
        {"name": "date", "display_name": "Date", "color": "#2563eb", "tools": ["bbox"]},
    ],
    "classification": [],
}


class TestPredictPdfSpans:
    def _predict(self, httpserver: Any, data: bytes) -> AnnotationResult:
        httpserver.expect_request("/doc.pdf").respond_with_data(
            data, content_type="application/pdf"
        )
        item = {
            "id": "doc",
            "url": httpserver.url_for("/doc.pdf"),
            "width": 0,
            "height": 0,
            "media_type": "pdf",
        }
        with TestClient(app) as client:
            response = client.post("/predict", json={"items": [item], "schema": SPAN_SCHEMA})
        assert response.status_code == 200, response.text
        return AnnotationResult.model_validate(response.json()["predictions"][0]["result"])

    def test_person_span_has_page_boxes_and_text(self, httpserver: Any) -> None:
        result = self._predict(httpserver, pdf(["Invoice sent to Mr. Anna Virtanen on 28.09.2026"]))
        spans = [s for s in result.shapes if s.type == "span"]
        assert spans, result
        [span] = [s for s in spans if s.text and "Virtanen" in s.text]
        assert span.class_ == "PER"
        assert span.page == 1
        assert span.start is None and span.end is None
        assert span.boxes is not None and len(span.boxes) == 1
        # Bbox fields still work alongside.
        assert [s.text for s in result.shapes if s.type == "bbox"] == ["28.09.2026"]

    def test_line_break_ends_an_entity(self, httpserver: Any) -> None:
        # text_ner joins names only across a single space, and a lone word that
        # opens a line is no entity, so a name wrapped onto the next line keeps
        # only its first line.
        result = self._predict(httpserver, pdf(["Contact Anna\nVirtanen today"]))
        spans = [s for s in result.shapes if s.type == "span"]
        assert [s.text for s in spans] == ["Contact Anna"]
        assert all(s.boxes is not None and len(s.boxes) == 1 for s in spans)
