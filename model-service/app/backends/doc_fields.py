"""Rule-based document fields for PDF items (reference pre-labelling).

Deliberately simple, like `text_ner`: it exists so the whole PDF path —
worker → `/predict` → boxes with `page` and `text` → annotator — can be
exercised without a trained model, not to be good at invoices.

Words come from PDFium's text layer (`pdf_words`) in the page's own PDF
points, top-left origin, y down, `/Rotate` applied — the space pdf shapes
use (CONTRACTS.md "PDF items"). `find_fields` then marks amounts, dates and
e-mail addresses. A scanned page has no text layer and yields nothing.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

import pypdfium2 as pdfium

#: The class names this module can predict, in the order they are tried.
LABELS = ("amount", "date", "email")

_AMOUNT = re.compile(r"^[€$£]?\d{1,3}(?:[.,\s]?\d{3})*[.,]\d{2}(?:€|EUR|USD|\$)?$", re.I)
_CURRENCY = re.compile(r"^(?:€|\$|£|EUR|USD|GBP)$", re.I)
_DATE = re.compile(
    r"^(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}-\d{2}-\d{2})$",
)
_EMAIL = re.compile(r"^[\w.+-]+@[\w-]+(?:\.[\w-]+)+$")
_TRIM = ".,;:()[]"

#: PDFium is not thread-safe; the service may run predictions concurrently.
PDFIUM_LOCK = threading.Lock()


@dataclass(frozen=True)
class Word:
    page: int
    text: str
    bbox: tuple[float, float, float, float]
    #: The whitespace before this word held a line break (text layer only).
    newline_before: bool = False


@dataclass(frozen=True)
class Field:
    label: str
    page: int
    text: str
    bbox: tuple[float, float, float, float]
    score: float


def _to_view(
    x: float, y: float, box: tuple[float, float, float, float], rotation: int
) -> tuple[float, float]:
    """PDF user space (y up) → top-left, y-down page points after `/Rotate`."""
    x0, y0, x1, y1 = box
    if rotation == 90:
        return y - y0, x - x0
    if rotation == 180:
        return x1 - x, y - y0
    if rotation == 270:
        return y1 - y, x1 - x
    return x - x0, y1 - y


_Char = tuple[str, tuple[float, float, float, float]]


def _flush(chars: list[_Char], page: int, words: list[Word], newline: bool = False) -> None:
    """Close the word being built from `chars` (if any) into `words`."""
    if not chars:
        return
    xs = [c[1][0] for c in chars] + [c[1][2] for c in chars]
    ys = [c[1][1] for c in chars] + [c[1][3] for c in chars]
    words.append(
        Word(
            page=page,
            text="".join(c[0] for c in chars),
            bbox=(min(xs), min(ys), max(xs), max(ys)),
            newline_before=newline,
        )
    )
    chars.clear()


def pdf_words(data: bytes, *, max_pages: int = 50) -> list[Word]:
    """The words of the first `max_pages` pages, split on whitespace."""
    words: list[Word] = []
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(data)
        try:
            for index in range(min(len(document), max_pages)):
                page = document[index]
                textpage = page.get_textpage()
                try:
                    box = page.get_cropbox()  # pdf.js places the page by its CropBox
                    rotation = page.get_rotation()
                    current: list[_Char] = []
                    pending_break = False  # a line break since the last word ended
                    word_break = False  # ... as it was when the current word began
                    for i in range(textpage.count_chars()):
                        char = textpage.get_text_range(i, 1)
                        if not char or char.isspace():
                            if current:
                                _flush(current, index + 1, words, word_break)
                                pending_break = False
                            pending_break = pending_break or char in ("\r", "\n")
                            continue
                        if not current:
                            word_break = pending_break and any(w.page == index + 1 for w in words)
                        left, bottom, right, top = textpage.get_charbox(i)
                        ax, ay = _to_view(left, bottom, box, rotation)
                        bx, by = _to_view(right, top, box, rotation)
                        current.append((char, (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))))
                    _flush(current, index + 1, words, word_break)
                finally:
                    textpage.close()
                    page.close()
        finally:
            document.close()
    return words


def _union(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, float, float, float]:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def find_fields(words: list[Word]) -> list[Field]:
    """Amounts (with an adjacent currency word merged in), dates and e-mails."""
    fields: list[Field] = []
    used: set[int] = set()
    for i, word in enumerate(words):
        if i in used:
            continue
        token = word.text.strip(_TRIM)
        if not token:
            continue
        if _EMAIL.match(token):
            fields.append(Field("email", word.page, token, word.bbox, 0.9))
        elif _DATE.match(token):
            fields.append(Field("date", word.page, token, word.bbox, 0.8))
        elif _AMOUNT.match(token):
            text, bbox = token, word.bbox
            after = words[i + 1] if i + 1 < len(words) else None
            before = words[i - 1] if i > 0 else None
            if after and after.page == word.page and _CURRENCY.match(after.text.strip(_TRIM)):
                text, bbox = f"{text} {after.text.strip(_TRIM)}", _union(bbox, after.bbox)
                used.add(i + 1)
            elif (
                before
                and i - 1 not in used
                and before.page == word.page
                and _CURRENCY.match(before.text.strip(_TRIM))
            ):
                text, bbox = f"{before.text.strip(_TRIM)} {text}", _union(before.bbox, bbox)
            fields.append(Field("amount", word.page, text, bbox, 0.85))
    return fields


def _centre(word: Word) -> float:
    return (word.bbox[1] + word.bbox[3]) / 2


def page_text(words: list[Word]) -> tuple[str, list[tuple[int, int]]]:
    """One page's words joined as the platform export does, with each word's range.

    Words are joined by " ", or by "\n" where the whitespace between them held a
    line break. Returns the text and, per word, its `[start, end)` in that text.
    """
    parts: list[str] = []
    ranges: list[tuple[int, int]] = []
    length = 0
    for i, word in enumerate(words):
        if i:
            sep = "\n" if word.newline_before else " "
            parts.append(sep)
            length += 1
        ranges.append((length, length + len(word.text)))
        parts.append(word.text)
        length += len(word.text)
    return "".join(parts), ranges


def words_in_range(ranges: list[tuple[int, int]], start: int, end: int) -> list[int]:
    """Indexes of the words whose range overlaps `[start, end)`."""
    return [i for i, (a, b) in enumerate(ranges) if a < end and b > start]


def line_boxes(words: list[Word]) -> list[tuple[float, float, float, float]]:
    """One box per line: consecutive words whose vertical centres lie within half
    a word's height of each other form a line; the box is the union of their bboxes.
    """
    boxes: list[tuple[float, float, float, float]] = []
    previous: Word | None = None
    for word in words:
        if previous is not None:
            height = max(previous.bbox[3] - previous.bbox[1], word.bbox[3] - word.bbox[1])
            if abs(_centre(word) - _centre(previous)) <= height / 2:
                boxes[-1] = _union(boxes[-1], word.bbox)
                previous = word
                continue
        boxes.append(word.bbox)
        previous = word
    return boxes
