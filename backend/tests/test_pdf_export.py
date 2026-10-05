"""spaCy / CoNLL export of pdf items: text-layer words, offsets, warnings (EXP-5)."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.exporters import ConllExporter, ExportItem, SpacyExporter
from app.exporters.base import entity_set_for_item, pdf_document, split_text_items
from app.models import JobType, MediaType
from app.schemas import AnnotationResult, LabelSchemaDefinition
from app.schemas.annotation import RelationShape, SpanShape
from app.schemas.label_schema import ClassDef, ToolType
from app.schemas.pdf_text import PdfWord
from app.services import pdf_text
from app.services.pdf_text import PdfTextError, extract_pdf_words
from app.worker import jobs
from tests.test_worker import (  # noqa: F401 - fixtures
    _run,
    _seed_job,
    _seed_project,
    _seed_text_item,
    ctx,
    engine,
    sessionmaker,
)

DEFINITION = LabelSchemaDefinition(
    version=1,
    classes=[
        ClassDef(name="PER", display_name="Person", color="#e11d48", tools=[ToolType.SPAN]),
    ],
)


def make_pdf(
    pages: list[list[tuple[int, int, str]]],
    *,
    rotate: dict[int, int] | None = None,
    crop: tuple[int, int, int, int] | None = None,
) -> bytes:
    """A 600 x 800 pt PDF; each page is a list of `(x, y, text)` lines in Helvetica 12."""
    rotate = rotate or {}
    objects: dict[int, str] = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    kids = []
    for index, lines in enumerate(pages):
        page_id, content_id = 4 + 2 * index, 5 + 2 * index
        stream = "".join(f"BT /F1 12 Tf {x} {y} Td ({text}) Tj ET\n" for x, y, text in lines)
        objects[content_id] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
        extra = f" /Rotate {rotate[index]}" if index in rotate else ""
        if crop:
            extra += f" /CropBox [{' '.join(map(str, crop))}]"
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800]{extra} "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        )
        kids.append(f"{page_id} 0 R")
    objects[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"
    body = b"%PDF-1.4\n"
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(body)
        body += f"{number} 0 obj\n{objects[number]}\nendobj\n".encode("latin-1")
    xref = len(body)
    size = max(objects) + 1
    body += f"xref\n0 {size}\n0000000000 65535 f \n".encode()
    for number in range(1, size):
        body += f"{offsets[number]:010d} 00000 n \n".encode()
    body += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return body


#: Two pages: two lines on the first, one on the second.
PAGES = [
    [(100, 700, "Anna Smith lives"), (100, 680, "in Paris today")],
    [(100, 700, "Second page")],
]


def boxes_of(
    words: list[PdfWord], first: int, last: int
) -> list[tuple[float, float, float, float]]:
    """One box per line over `words[first..last]`, padded a little like the annotator's."""
    boxes: dict[float, list[float]] = {}
    for word in words[first : last + 1]:
        x0, y0, x1, y1 = word.bbox
        line = boxes.setdefault(round((y0 + y1) / 2), [x0, y0, x1, y1])
        line[0], line[1] = min(line[0], x0), min(line[1], y0)
        line[2], line[3] = max(line[2], x1), max(line[3], y1)
    return [(b[0] - 1, b[1] - 1, b[2] + 1, b[3] + 1) for b in boxes.values()]


def pdf_span(
    words: list[PdfWord], first: int, last: int, class_: str = "PER", page: int = 1
) -> SpanShape:
    return SpanShape(
        id=uuid.uuid4(),
        **{"class": class_},
        page=page,
        boxes=boxes_of(words, first, last),
    )


def pdf_item(
    words: list[PdfWord] | None, shapes: list[Any], path: str = "docs/a.pdf"
) -> ExportItem:
    result = AnnotationResult(
        schema_version=1, media_type=MediaType.PDF, classification={}, shapes=shapes
    )
    return ExportItem(
        id=uuid.uuid4(), path=path, width=None, height=None, result=result, pdf_words=words
    )


@pytest.fixture(scope="module")
def words() -> list[PdfWord]:
    return extract_pdf_words(make_pdf(PAGES))


def test_extraction_marks_line_breaks_and_pages(words: list[PdfWord]) -> None:
    assert [w.text for w in words] == [
        "Anna", "Smith", "lives", "in", "Paris", "today", "Second", "page",
    ]  # fmt: skip
    assert [w.page for w in words] == [1] * 6 + [2] * 2
    assert [w.line_break_before for w in words][:6] == [False, False, False, True, False, False]
    x0, y0, x1, y1 = words[0].bbox
    assert 99 < x0 < x1 and 85 < y0 < y1 < 105  # top-left origin, y down


def test_document_text_joins_words_lines_and_pages(words: list[PdfWord]) -> None:
    text, ranges = pdf_document(words)
    assert text == "Anna Smith lives\nin Paris today\n\nSecond page"
    assert [text[a:b] for a, b in ranges] == [w.text for w in words]


def test_rotated_page_uses_the_viewed_coordinates() -> None:
    rotated = extract_pdf_words(make_pdf([[(100, 700, "Turned")]], rotate={0: 90}))
    x0, y0, x1, y1 = rotated[0].bbox
    # /Rotate 90 makes the page 800 wide and 600 tall.
    assert 0 <= x0 < x1 <= 800 and 0 <= y0 < y1 <= 600
    assert x0 > 600


def test_crop_box_is_the_page_origin_as_in_pdf_js() -> None:
    plain = extract_pdf_words(make_pdf([[(100, 700, "Cropped")]]))[0].bbox
    cropped = extract_pdf_words(make_pdf([[(100, 700, "Cropped")]], crop=(50, 0, 550, 750)))
    x0, y0, x1, y1 = cropped[0].bbox
    # The crop moves the origin 50 pt right and the top edge 50 pt down.
    assert (x0, y0, x1, y1) == pytest.approx(
        (plain[0] - 50, plain[1] - 50, plain[2] - 50, plain[3] - 50)
    )


def test_code_points_join_surrogate_pairs_and_keep_odd_units_in_the_word() -> None:
    # "a", U+1D400 as a surrogate pair, a lone low surrogate, 0, "b"
    assert pdf_text.code_points([0x61, 0xD835, 0xDC00, 0xDC01, 0, 0x62]) == [
        ("a", [0]),
        ("\U0001d400", [1, 2]),
        ("\ufffd", [3]),
        (" ", [4]),
        ("b", [5]),
    ]


def test_documents_over_the_limits_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    data = make_pdf(PAGES)
    monkeypatch.setattr(pdf_text, "MAX_PAGES", 1)
    with pytest.raises(PdfTextError, match="2 pages"):
        extract_pdf_words(data)
    monkeypatch.setattr(pdf_text, "MAX_PAGES", 10)
    monkeypatch.setattr(pdf_text, "MAX_WORDS", 3)
    with pytest.raises(PdfTextError, match="over 3 words"):
        extract_pdf_words(data)


def test_unreadable_pdf_raises() -> None:
    with pytest.raises(PdfTextError):
        extract_pdf_words(b"%PDF-1.4 not really")


def test_scan_without_text_layer_has_no_words() -> None:
    assert extract_pdf_words(make_pdf([[]])) == []


def test_span_becomes_an_offset_range(words: list[PdfWord]) -> None:
    item = pdf_item(words, [pdf_span(words, 0, 1)])
    entities, warnings = entity_set_for_item(item)
    assert [(e.start, e.end, e.class_) for e in entities] == [(0, 10, "PER")]
    assert warnings == []


def test_multi_line_span_covers_the_line_break(words: list[PdfWord]) -> None:
    item = pdf_item(words, [pdf_span(words, 2, 4)])
    entities, warnings = entity_set_for_item(item)
    text, _ = pdf_document(words)
    assert [text[e.start : e.end] for e in entities if e.start is not None and e.end] == [
        "lives\nin Paris"
    ]
    assert warnings == []


def test_span_on_a_later_page_uses_document_offsets(words: list[PdfWord]) -> None:
    shape = SpanShape(
        id=uuid.uuid4(),
        **{"class": "PER"},
        page=2,
        boxes=boxes_of(words, 6, 7),
    )
    entities, _ = entity_set_for_item(pdf_item(words, [shape]))
    text, _ = pdf_document(words)
    assert text[entities[0].offsets[0] : entities[0].offsets[1]] == "Second page"


def test_span_over_blank_space_is_skipped_with_a_warning(words: list[PdfWord]) -> None:
    blank = SpanShape(
        id=uuid.uuid4(), **{"class": "PER"}, page=1, boxes=[(400.0, 400.0, 500.0, 420.0)]
    )
    item = pdf_item(words, [blank, pdf_span(words, 0, 0)])
    entities, warnings = entity_set_for_item(item)
    assert len(entities) == 1
    assert warnings == ["docs/a.pdf: 1 pdf span(s) skipped, covering no word"]


def test_range_including_an_uncovered_word_warns_but_exports(words: list[PdfWord]) -> None:
    # Boxes over "Anna" and "lives" only: the range takes in "Smith" as well.
    shape = SpanShape(
        id=uuid.uuid4(),
        **{"class": "PER"},
        page=1,
        boxes=[*boxes_of(words, 0, 0), *boxes_of(words, 2, 2)],
    )
    entities, warnings = entity_set_for_item(pdf_item(words, [shape]))
    assert [e.offsets for e in entities] == [(0, 16)]
    assert warnings == ["docs/a.pdf: 1 pdf span(s) take in words outside their boxes"]


def test_split_keeps_pdf_items_with_words_and_reports_the_rest(words: list[PdfWord]) -> None:
    kept, warnings = split_text_items(
        [pdf_item(words, []), pdf_item(None, [], "docs/b.pdf"), pdf_item([], [], "docs/c.pdf")]
    )
    assert [i.path for i in kept] == ["docs/a.pdf", "docs/c.pdf"]
    assert warnings == ["docs/b.pdf: skipped, source text could not be read"]


def test_spacy_line_for_a_pdf_item(words: list[PdfWord]) -> None:
    anna = pdf_span(words, 0, 1)
    paris = pdf_span(words, 4, 4, "LOC")
    box = {"id": str(uuid.uuid4()), "type": "bbox", "class": "x", "page": 1, "x": 1, "y": 1,
           "w": 5, "h": 5}  # fmt: skip
    shapes: list[Any] = [
        anna,
        paris,
        RelationShape.model_validate(
            {
                "id": str(uuid.uuid4()),
                "type": "relation",
                "class": "in",
                "from": anna.id,
                "to": paris.id,
            }
        ),
        RelationShape.model_validate(
            {
                "id": str(uuid.uuid4()),
                "type": "relation",
                "class": "at",
                "from": anna.id,
                "to": box["id"],
            }
        ),
    ]
    item = pdf_item(words, shapes)
    files = {f.path: f.data for f in SpacyExporter().export([item], DEFINITION)}
    record = json.loads(files["annotations.jsonl"])
    assert record["text"] == "Anna Smith lives\nin Paris today\n\nSecond page"
    assert record["entities"] == [[0, 10, "PER"], [20, 25, "LOC"]]
    assert record["spans"] == {"sc": [[0, 10, "PER"], [20, 25, "LOC"]]}
    assert record["relations"] == [[0, 1, "in"]]  # the relation to a non-span is dropped
    assert record["meta"] == {"item_id": str(item.id), "path": "docs/a.pdf", "media_type": "pdf"}
    assert "warnings.json" not in files


def test_conll_for_a_pdf_item(words: list[PdfWord]) -> None:
    item = pdf_item(words, [pdf_span(words, 0, 1), pdf_span(words, 4, 4, "LOC")])
    files = {f.path: f.data for f in ConllExporter().export([item], DEFINITION)}
    lines = files["annotations.conll"].decode().splitlines()
    assert lines[:2] == [f"# item_id = {item.id}", "# path = docs/a.pdf"]
    assert lines[2:9] == [
        "Anna\tB-PER", "Smith\tI-PER", "lives\tO", "in\tO", "Paris\tB-LOC", "today\tO", "Second\tO",
    ]  # fmt: skip


def test_exporters_report_skipped_pdf_spans(words: list[PdfWord]) -> None:
    blank = SpanShape(
        id=uuid.uuid4(), **{"class": "PER"}, page=1, boxes=[(400.0, 400.0, 500.0, 420.0)]
    )
    files = {f.path: f.data for f in SpacyExporter().export([pdf_item(words, [blank])], DEFINITION)}
    assert json.loads(files["warnings.json"]) == [
        "docs/a.pdf: 1 pdf span(s) skipped, covering no word"
    ]


def test_worker_export_reads_pdfs_and_skips_unreadable_ones(
    ctx: dict[str, Any],  # noqa: F811
    sessionmaker: async_sessionmaker[AsyncSession],  # noqa: F811
    tmp_path: Path,
) -> None:
    fx = _seed_project(sessionmaker, tmp_path)
    (tmp_path / "docs").mkdir()
    data = make_pdf(PAGES)
    (tmp_path / "docs" / "a.pdf").write_bytes(data)
    (tmp_path / "docs" / "broken.pdf").write_bytes(b"%PDF-1.4 not really")
    parsed = extract_pdf_words(data)
    span = pdf_span(parsed, 0, 1).model_dump(mode="json", by_alias=True, exclude_none=True)
    item = _seed_text_item(sessionmaker, fx, "docs/a.pdf", [span], media_type=MediaType.PDF)
    _seed_text_item(sessionmaker, fx, "docs/broken.pdf", [], media_type=MediaType.PDF)
    _seed_text_item(sessionmaker, fx, "docs/big.pdf", [], size_bytes=65 * 1024 * 1024,
                    media_type=MediaType.PDF)  # fmt: skip

    job = _seed_job(sessionmaker, fx.project.id, JobType.EXPORT, {"format": "spacy", "filter": {}})
    result = _run(jobs.export(ctx, str(job.id)))

    assert any(w.startswith("docs/big.pdf: skipped") for w in result["warnings"])
    assert any(w.startswith("docs/broken.pdf: skipped") for w in result["warnings"])
    archive = zipfile.ZipFile(io.BytesIO((tmp_path / result["blob_path"]).read_bytes()))
    record = json.loads(archive.read("annotations.jsonl").decode().strip())
    assert record["entities"] == [[0, 10, "PER"]]
    assert record["meta"]["item_id"] == str(item.id)
    assert record["meta"]["media_type"] == "pdf"

    conll_job = _seed_job(
        sessionmaker, fx.project.id, JobType.EXPORT, {"format": "conll", "filter": {}}
    )
    conll = _run(jobs.export(ctx, str(conll_job.id)))
    conll_archive = zipfile.ZipFile(io.BytesIO((tmp_path / conll["blob_path"]).read_bytes()))
    assert "Anna\tB-PER" in conll_archive.read("annotations.conll").decode()
