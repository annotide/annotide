"""Words of a PDF's text layer for the spaCy / CoNLL export (EXP-5).

PDFium reads the customer's file; the text is never stored (ARC-3). Extraction
is synchronous and CPU-bound, so the worker runs :func:`extract_pdf_words` in a
thread (`asyncio.to_thread`). PDFium is not thread-safe: every use shares the
lock `thumbnails.py` takes, held per page so one long document does not stall
other jobs' thumbnails for its whole length.

Coordinates use the page's CropBox (MediaBox when it has none), as pdf.js
does when the annotator stores span boxes; characters are read as code
points (`FPDFText_GetUnicode`), since `get_text_range` is limited to UCS-2.
"""

from __future__ import annotations

import pypdfium2 as pdfium

from app.schemas.pdf_text import PdfWord
from app.services.thumbnails import PDFIUM_LOCK

_Char = tuple[str, tuple[float, float, float, float]]

#: A document over either limit is refused (`PdfTextError`), so the export
#: skips it with a warning instead of holding the worker for minutes.
MAX_PAGES = 2000
MAX_WORDS = 500_000


class PdfTextError(ValueError):
    """The bytes are not a PDF PDFium can open (undecodable, or encrypted), or
    the document is over `MAX_PAGES` / `MAX_WORDS`."""


def code_points(units: list[int]) -> list[tuple[str, list[int]]]:
    """PDFium's per-char values as `(character, char indices)`.

    A surrogate pair (two indices, on builds that report UTF-16) becomes one
    code point; a lone surrogate becomes U+FFFD, still part of its word; 0
    (no character) becomes a space, a word break.
    """
    out: list[tuple[str, list[int]]] = []
    i = 0
    while i < len(units):
        unit = units[i]
        if 0xD800 <= unit < 0xDC00 and i + 1 < len(units) and 0xDC00 <= units[i + 1] < 0xE000:
            point = 0x10000 + ((unit - 0xD800) << 10) + (units[i + 1] - 0xDC00)
            out.append((chr(point), [i, i + 1]))
            i += 2
            continue
        if unit == 0:
            out.append((" ", [i]))
        elif 0xD800 <= unit < 0xE000 or unit > 0x10FFFF:
            out.append(("\ufffd", [i]))
        else:
            out.append((chr(unit), [i]))
        i += 1
    return out


def _to_view(
    x: float, y: float, box: tuple[float, float, float, float], rotation: int
) -> tuple[float, float]:
    """PDF user space (y up) -> top-left, y-down page points after `/Rotate`."""
    x0, y0, x1, y1 = box
    if rotation == 90:
        return y - y0, x - x0
    if rotation == 180:
        return x1 - x, y - y0
    if rotation == 270:
        return y1 - y, x1 - x
    return x - x0, y1 - y


def _flush(chars: list[_Char], page: int, line_break: bool, words: list[PdfWord]) -> None:
    """Close the word being built from `chars` (if any) into `words`."""
    if not chars:
        return
    xs = [c[1][0] for c in chars] + [c[1][2] for c in chars]
    ys = [c[1][1] for c in chars] + [c[1][3] for c in chars]
    words.append(
        PdfWord(
            page=page,
            text="".join(c[0] for c in chars),
            bbox=(min(xs), min(ys), max(xs), max(ys)),
            line_break_before=line_break,
        )
    )
    chars.clear()


def _page_words(page: pdfium.PdfPage, number: int, words: list[PdfWord]) -> None:
    textpage = page.get_textpage()
    try:
        box = page.get_cropbox()
        rotation = page.get_rotation()
        current: list[_Char] = []
        break_before = False  # a line break in the whitespace before `current`
        pending_break = False
        units = [
            pdfium.raw.FPDFText_GetUnicode(textpage.raw, i) for i in range(textpage.count_chars())
        ]
        for char, indices in code_points(units):
            if char.isspace():
                if current:
                    _flush(current, number, break_before, words)
                    pending_break = False
                if char in ("\r", "\n"):
                    pending_break = True
                continue
            if not current:
                break_before = pending_break
            charboxes = [textpage.get_charbox(i) for i in indices]
            left = min(b[0] for b in charboxes)
            bottom = min(b[1] for b in charboxes)
            right = max(b[2] for b in charboxes)
            top = max(b[3] for b in charboxes)
            ax, ay = _to_view(left, bottom, box, rotation)
            bx, by = _to_view(right, top, box, rotation)
            current.append((char, (min(ax, bx), min(ay, by), max(ax, bx), max(ay, by))))
        _flush(current, number, break_before, words)
    finally:
        textpage.close()


def extract_pdf_words(data: bytes) -> list[PdfWord]:
    """The words of every page of `data` (see :func:`extract_pdf_document`)."""
    return extract_pdf_document(data)[1]


def extract_pdf_document(data: bytes) -> tuple[int, list[PdfWord]]:
    """The page count of `data` and the words of every page, split on whitespace,
    in PDFium's order.

    Raises :class:`PdfTextError` for files PDFium cannot open (undecodable or
    password-protected) and for documents over `MAX_PAGES` pages or
    `MAX_WORDS` words.
    """
    words: list[PdfWord] = []
    with PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(data)
        except pdfium.PdfiumError as exc:
            raise PdfTextError(f"cannot open pdf: {exc}") from exc
        page_count = len(document)
    try:
        if page_count > MAX_PAGES:
            raise PdfTextError(f"{page_count} pages, over the limit of {MAX_PAGES}")
        for index in range(page_count):
            with PDFIUM_LOCK:
                page = document[index]
                try:
                    _page_words(page, index + 1, words)
                finally:
                    page.close()
            if len(words) > MAX_WORDS:
                raise PdfTextError(f"over {MAX_WORDS} words")
    except pdfium.PdfiumError as exc:
        raise PdfTextError(f"cannot read pdf: {exc}") from exc
    finally:
        with PDFIUM_LOCK:
            document.close()
    return page_count, words


def group_ocr_lines(
    page: int, words: list[tuple[str, tuple[float, float, float, float]]]
) -> list[PdfWord]:
    """An OCR page's `(text, bbox)` words as :class:`PdfWord`, with line breaks.

    The span line rule (CONTRACTS.md *Entity spans on PDFs*): a word whose
    vertical centre lies within half the taller word's height of the previous
    word's starts no new line; any other word gets `line_break_before`.
    """
    out: list[PdfWord] = []
    previous: tuple[float, float] | None = None  # centre, height
    for text, bbox in words:
        centre = (bbox[1] + bbox[3]) / 2
        height = bbox[3] - bbox[1]
        new_line = previous is not None and abs(centre - previous[0]) > max(height, previous[1]) / 2
        out.append(PdfWord(page=page, text=text, bbox=bbox, line_break_before=new_line))
        previous = (centre, height)
    return out
