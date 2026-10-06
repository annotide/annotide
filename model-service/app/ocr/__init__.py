"""OCR for scanned documents: words and their boxes from page images.

A scanned PDF has no text layer, so neither the annotator nor the document
field rules (`backends/doc_fields`) have words to work with. An OCR engine
reads a rendered page instead. Engines are pluggable, like model backends,
and chosen by `OCR_ENGINE`:

- `tesseract` — the Tesseract binary, locally; nothing leaves the service.
- `anthropic` — Claude reads the page image (vision, structured output).
- `openai` — any OpenAI-compatible chat-completions endpoint with vision
  (OpenAI, Azure OpenAI, vLLM, Ollama, LM Studio, …).
- `auto` (default) — `tesseract` when the binary is installed, else none.
- `none` — no OCR: `/ocr` answers 501, scans yield no words.

The LLM engines send page images to the endpoint the operator configured.
That is the operator's choice of model (BYOM), like any other model endpoint.

Every engine answers in image pixels; `ocr_pdf` renders the pages and turns
the words into the page's points (top-left origin, `/Rotate` applied), the
space pdf shapes use (CONTRACTS.md "PDF items").
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from functools import cache
from typing import Protocol, runtime_checkable

import pypdfium2 as pdfium
from PIL import Image

from app.backends.doc_fields import PDFIUM_LOCK

#: Pages read per request or per pre-labelled item unless `OCR_MAX_PAGES` says otherwise.
DEFAULT_MAX_PAGES = 20


class OcrError(Exception):
    """The engine could not read a page (a crash, a timeout, a refusal, nonsense)."""


@dataclass(frozen=True)
class OcrWord:
    """One word; `bbox` is `(x0, y0, x1, y1)` in the image's (or page's) coordinates."""

    text: str
    bbox: tuple[float, float, float, float]
    confidence: float | None = None


@dataclass(frozen=True)
class OcrPage:
    """The words of one page, in PDF points; `page` is 1-based."""

    page: int
    width: float
    height: float
    words: list[OcrWord]


@runtime_checkable
class OcrEngine(Protocol):
    """Reads the words of one page image."""

    name: str
    #: Resolution pages are rendered at, and a cap on the longer side in pixels.
    dpi: int
    max_side: int

    async def read(self, image: Image.Image) -> list[OcrWord]:
        """Words in `image`, boxes in its pixels. Raises `OcrError` on failure."""
        ...


def max_pages() -> int:
    return max(1, int(os.environ.get("OCR_MAX_PAGES", str(DEFAULT_MAX_PAGES))))


@cache
def get_engine(name: str | None = None) -> OcrEngine | None:
    """The engine named by `name` or `OCR_ENGINE`; None when OCR is off.

    Cached: the LLM engines hold an HTTP client. Raises `ValueError` for an
    unknown name or a missing setting, so a bad configuration fails at start-up.
    """
    choice = (name or os.environ.get("OCR_ENGINE", "auto")).strip().lower()
    if choice == "auto":
        choice = "tesseract" if shutil.which(os.environ.get("TESSERACT_BIN", "tesseract")) else ""
    if choice in ("", "none"):
        return None
    if choice == "tesseract":
        from app.ocr.tesseract import TesseractEngine

        return TesseractEngine.from_env()
    if choice == "anthropic":
        from app.ocr.llm import AnthropicEngine

        return AnthropicEngine.from_env()
    if choice == "openai":
        from app.ocr.llm import OpenAICompatibleEngine

        return OpenAICompatibleEngine.from_env()
    raise ValueError(
        f"unknown OCR_ENGINE '{choice}': expected auto, none, tesseract, anthropic or openai"
    )


@dataclass(frozen=True)
class _Rendered:
    page: int
    width: float
    height: float
    image: Image.Image


def page_count_or_error(data: bytes) -> int:
    """Pages in the PDF; `ValueError` when it cannot be opened."""
    with PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(data)
        except pdfium.PdfiumError as exc:
            raise ValueError(f"could not read pdf: {exc}") from exc
        try:
            return len(document)
        finally:
            document.close()


def _render(data: bytes, pages: list[int], dpi: int, max_side: int) -> list[_Rendered]:
    rendered: list[_Rendered] = []
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(data)
        try:
            count = len(document)
            for number in pages:
                if not 1 <= number <= count:
                    raise ValueError(f"page {number} is outside 1..{count}")
                page = document[number - 1]
                try:
                    # PDFium reports the size, and renders, with `/Rotate` applied.
                    width, height = page.get_size()
                    scale = min(dpi / 72, max_side / max(width, height, 1.0))
                    bitmap = page.render(scale=scale)
                    image = bitmap.to_pil().convert("RGB")
                    bitmap.close()
                finally:
                    page.close()
                rendered.append(_Rendered(number, width, height, image))
        finally:
            document.close()
    return rendered


def _clamped(
    words: list[OcrWord], sx: float, sy: float, width: float, height: float
) -> list[OcrWord]:
    """Words scaled by (sx, sy) and clamped to the page; empty or degenerate ones dropped."""
    out: list[OcrWord] = []
    for word in words:
        text = " ".join(word.text.split())
        x0, y0, x1, y1 = word.bbox
        box = (
            min(max(min(x0, x1) * sx, 0.0), width),
            min(max(min(y0, y1) * sy, 0.0), height),
            min(max(max(x0, x1) * sx, 0.0), width),
            min(max(max(y0, y1) * sy, 0.0), height),
        )
        if text and box[2] > box[0] and box[3] > box[1]:
            rounded = (round(box[0], 2), round(box[1], 2), round(box[2], 2), round(box[3], 2))
            out.append(OcrWord(text, rounded, word.confidence))
    return out


async def ocr_pdf(data: bytes, pages: list[int], engine: OcrEngine) -> list[OcrPage]:
    """Render `pages` (1-based) of the PDF and read them, one after another.

    Sequential on purpose: an external LLM rate-limits, and Tesseract already
    uses the cores it gets. Raises `ValueError` for an unreadable PDF or a page
    out of range, `OcrError` when the engine fails.
    """
    try:
        rendered = await asyncio.to_thread(_render, data, pages, engine.dpi, engine.max_side)
    except pdfium.PdfiumError as exc:
        raise ValueError(f"could not read pdf: {exc}") from exc
    result: list[OcrPage] = []
    for page in rendered:
        words = await engine.read(page.image)
        sx = page.width / page.image.width
        sy = page.height / page.image.height
        result.append(
            OcrPage(
                page.page,
                round(page.width, 2),
                round(page.height, 2),
                _clamped(words, sx, sy, page.width, page.height),
            )
        )
    return result


async def ocr_image(image: Image.Image, engine: OcrEngine) -> OcrPage:
    """Read an image item; words stay in its pixels (downscaled first if huge)."""
    width, height = image.size
    scaled = image.convert("RGB")
    if max(width, height) > engine.max_side:
        factor = engine.max_side / max(width, height)
        scaled = scaled.resize((max(1, round(width * factor)), max(1, round(height * factor))))
    words = await engine.read(scaled)
    return OcrPage(
        1,
        float(width),
        float(height),
        _clamped(words, width / scaled.width, height / scaled.height, width, height),
    )
