"""Exporter protocol, shared data structures and the format registry."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from typing import ClassVar, Protocol
from uuid import UUID

from app.schemas.annotation import AnnotationResult, Shape, SpanShape
from app.schemas.item import MediaType
from app.schemas.label_schema import LabelSchemaDefinition
from app.schemas.pdf_text import PdfWord

#: Media types COCO/YOLO (image-only formats) cannot represent (CONTRACTS.md
#: *Annotation result JSON*: "exporters that only understand images ... skip
#: text and video items and count them in the job's warnings").
_NON_IMAGE_MEDIA_TYPES = (
    MediaType.TEXT,
    MediaType.VIDEO,
    MediaType.PDF,
    MediaType.AUDIO,
    MediaType.LLM,
    MediaType.TIMESERIES,
)


@dataclass(frozen=True, slots=True)
class ExportItem:
    """One media item plus its current annotation result, ready to export.

    `text` is the item's source content, filled in by the worker for `text`
    and `llm` items (EXP-5) — exporters never touch connectors themselves, per
    the dependency rule in docs/CONTRACTS.md. `None` means the item is not
    text, or the worker could not read it (too large, or a connector error).
    `pdf_words` is the same for `pdf` items: the words of the PDF's text layer
    (empty for a scan), `None` when the worker could not read the file.
    """

    id: UUID
    path: str
    width: int | None
    height: int | None
    result: AnnotationResult
    text: str | None = None
    pdf_words: list[PdfWord] | None = None


@dataclass(frozen=True, slots=True)
class ExportFile:
    """One file produced by an exporter, relative to the export archive root."""

    path: str
    data: bytes


class Exporter(Protocol):
    """A format-specific converter from annotation results to export files."""

    format: ClassVar[str]

    def export(
        self, items: Iterable[ExportItem], definition: LabelSchemaDefinition
    ) -> Iterator[ExportFile]:
        """Convert `items` into one or more files for this export format."""
        ...


EXPORTERS: dict[str, Exporter] = {}


def register_exporter(exporter_cls: type[Exporter]) -> type[Exporter]:
    """Class decorator: instantiate `exporter_cls` and register it under its `format`."""
    EXPORTERS[exporter_cls.format] = exporter_cls()
    return exporter_cls


def get_exporter(format: str) -> Exporter:  # noqa: A002 - matches the contract's parameter name
    """Look up a registered exporter by format name."""
    try:
        return EXPORTERS[format]
    except KeyError as exc:
        raise KeyError(f"unknown export format: '{format}'") from exc


def split_image_items(items: Iterable[ExportItem]) -> tuple[list[ExportItem], list[str]]:
    """Split `items` into image-compatible ones and a warning per text/video/pdf item skipped.

    COCO and YOLO only understand image geometry; `text` and `video` results
    (spans/relations, or `frame`/`track_id`) have no representation in either
    format and are skipped rather than crashing the export.
    """
    kept: list[ExportItem] = []
    warnings: list[str] = []
    for item in items:
        if item.result.media_type in _NON_IMAGE_MEDIA_TYPES:
            warnings.append(
                f"{item.path}: skipped {item.result.media_type} item, not supported by this format"
            )
        else:
            kept.append(item)
    return kept, warnings


def warnings_file(warnings: list[str]) -> ExportFile | None:
    """`warnings.json` listing skipped items, or `None` when nothing was skipped."""
    if not warnings:
        return None
    return ExportFile(path="warnings.json", data=json.dumps(warnings, indent=2).encode("utf-8"))


def split_text_items(items: Iterable[ExportItem]) -> tuple[list[ExportItem], list[str]]:
    """Split `items` into text/pdf items with readable content and a warning per skip.

    `spacy` and `conll` understand `text` items and, through their text layer,
    `pdf` items (EXP-5). An item is skipped, with a warning, when its media
    type is neither or when the worker could not read its source content (text
    over 16 MiB, a pdf over 64 MiB, undecodable or encrypted, or a connector
    error) — all surface as a missing `text` / `pdf_words`, so there is one
    message.
    """
    kept: list[ExportItem] = []
    warnings: list[str] = []
    for item in items:
        media_type = item.result.media_type
        if media_type not in (MediaType.TEXT, MediaType.PDF):
            warnings.append(f"{item.path}: skipped {media_type} item, not supported by this format")
        elif (item.pdf_words if media_type is MediaType.PDF else item.text) is None:
            warnings.append(f"{item.path}: skipped, source text could not be read")
        else:
            kept.append(item)
    return kept, warnings


def pdf_document(words: list[PdfWord]) -> tuple[str, list[tuple[int, int]]]:
    """The document text of a pdf's `words` and each word's `[start, end)` in it.

    Words are joined by a space, or by `\n` where the whitespace before them
    held a line break; pages are joined by `\n\n` (CONTRACTS.md *PDF items*).
    """
    parts: list[str] = []
    ranges: list[tuple[int, int]] = []
    position = 0
    previous: PdfWord | None = None
    for word in words:
        if previous is not None:
            if word.page != previous.page:
                separator = "\n\n"
            else:
                separator = "\n" if word.line_break_before else " "
            parts.append(separator)
            position += len(separator)
        ranges.append((position, position + len(word.text)))
        parts.append(word.text)
        position += len(word.text)
        previous = word
    return "".join(parts), ranges


def _covered_words(span: SpanShape, words: list[PdfWord]) -> list[int]:
    """Indices of the words on the span's page whose centre lies in one of its boxes."""
    covered: list[int] = []
    for index, word in enumerate(words):
        if word.page != span.page:
            continue
        cx = (word.bbox[0] + word.bbox[2]) / 2
        cy = (word.bbox[1] + word.bbox[3]) / 2
        if any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in span.boxes or ()):
            covered.append(index)
    return covered


def prepare_text_item(item: ExportItem) -> tuple[ExportItem, list[str]]:
    """A `pdf` item as a text item: document text plus offset spans, with warnings.

    Each pdf span becomes the code-point range from its first to its last
    covered word, keeping its id, class and attributes so relations still
    resolve. A span covering no word is dropped; a range that takes in words
    outside the span's boxes is kept and counted. Any other item (text, or one
    already prepared) comes back unchanged, so this is idempotent.
    """
    if item.result.media_type is not MediaType.PDF or item.pdf_words is None:
        return item, []
    text, ranges = pdf_document(item.pdf_words)
    shapes: list[Shape] = []
    empty = 0
    loose = 0
    for shape in item.result.shapes:
        if not isinstance(shape, SpanShape):
            shapes.append(shape)
            continue
        covered = _covered_words(shape, item.pdf_words)
        if not covered:
            empty += 1
            continue
        first, last = covered[0], covered[-1]
        if last - first + 1 != len(covered):
            loose += 1
        shapes.append(
            shape.model_copy(
                update={"start": ranges[first][0], "end": ranges[last][1], "boxes": None}
            )
        )
    warnings: list[str] = []
    if empty:
        warnings.append(f"{item.path}: {empty} pdf span(s) skipped, covering no word")
    if loose:
        warnings.append(f"{item.path}: {loose} pdf span(s) take in words outside their boxes")
    # `model_copy` skips validation on purpose: the spans now carry offsets on a pdf result.
    result = item.result.model_copy(update={"shapes": shapes})
    return replace(item, text=text, result=result, pdf_words=None), warnings


def build_entity_set(spans: Iterable[SpanShape]) -> tuple[list[SpanShape], int]:
    """The item's entity set (CONTRACTS.md, text export formats): `spans` kept

    greedily longest-first (ties: earlier start) so that none overlap.
    Returns the kept spans in text order plus the count dropped for overlap.
    Spans carry offsets (a pdf span goes through `prepare_text_item` first).
    """
    ordered = sorted(spans, key=lambda span: (-(span.offsets[1] - span.offsets[0]), span.offsets))
    kept: list[SpanShape] = []
    dropped = 0
    for span in ordered:
        start, end = span.offsets
        if any(start < other.offsets[1] and other.offsets[0] < end for other in kept):
            dropped += 1
        else:
            kept.append(span)
    kept.sort(key=lambda span: span.offsets)
    return kept, dropped


def entity_set_for_item(item: ExportItem) -> tuple[list[SpanShape], list[str]]:
    """`build_entity_set` over one item's spans, plus the warnings for the item.

    For a pdf item (not yet prepared) the warnings include those of
    `prepare_text_item`.
    """
    item, warnings = prepare_text_item(item)
    spans = [shape for shape in item.result.shapes if isinstance(shape, SpanShape)]
    kept, dropped = build_entity_set(spans)
    if dropped:
        warnings.append(f"{item.path}: {dropped} span(s) dropped for overlapping")
    return kept, warnings
