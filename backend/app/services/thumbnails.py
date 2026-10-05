"""Thumbnail generation for the data browser (IMG-8, UX-6).

The grid used to preview the full object through its signed `media_url`,
which is correct but pulls a 20 MB photo for a 200 px tile. The `thumbnail`
job downscales each image once and stores the result as *derived data* under
`cache/thumbnails/` on the project's cache connector — safe to delete and
regenerate (SRC-6), and never a copy of the raw media (ARC-3: a 256 px JPEG
is a new artefact, not the customer's file).

Pillow decoding is synchronous and CPU-bound; callers run
:func:`render_thumbnail` in a thread (`asyncio.to_thread`) so the worker's
event loop keeps reporting progress and answering aborts.

PDF items get the same treatment from their first page
(:func:`render_pdf_thumbnail`), which also measures the document for
`item.meta`: page count, page sizes and whether it has a text layer — never
the text itself, which stays in the customer's file (ARC-3).
"""

from __future__ import annotations

import io
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pypdfium2 as pdfium
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Item, MediaType

THUMBNAIL_CONTENT_TYPE = "image/jpeg"
JPEG_QUALITY = 82


class ThumbnailError(ValueError):
    """The source bytes are not an image Pillow can decode safely."""


def thumbnail_blob_path(item_id: UUID) -> str:
    """Where an item's thumbnail lives on the cache connector (see *Blob layout*)."""
    return f"cache/thumbnails/{item_id}.jpg"


def render_thumbnail(data: bytes, size: int) -> bytes:
    """Downscale `data` to fit in a `size` x `size` box and encode it as JPEG.

    Honours EXIF orientation, flattens transparency onto white (JPEG has no
    alpha) and uses the JPEG decoder's reduced-size draft mode where it
    applies, so a 6000 px photo is never fully decoded just to make a 256 px
    tile. Raises :class:`ThumbnailError` for undecodable input and for images
    Pillow refuses as decompression bombs.
    """
    if size < 1:
        raise ValueError("thumbnail size must be positive")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.draft("RGB", (size, size))
            oriented = ImageOps.exif_transpose(image) or image
            if oriented.mode in ("RGBA", "LA") or (
                oriented.mode == "P" and "transparency" in oriented.info
            ):
                rgba = oriented.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                oriented = flat
            elif oriented.mode != "RGB":
                oriented = oriented.convert("RGB")
            oriented.thumbnail((size, size), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            oriented.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            return out.getvalue()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise ThumbnailError(f"cannot decode image: {exc}") from exc


#: PDFium is not thread-safe, and two worker jobs can render at once.
PDFIUM_LOCK = threading.Lock()
#: `meta.page_sizes` stops here; `meta.page_count` is always exact.
MAX_PAGE_SIZES = 2000


@dataclass(frozen=True)
class PdfThumbnail:
    jpeg: bytes
    #: Merged into `item.meta` (CONTRACTS.md "PDF items").
    meta: dict[str, Any]


def _encode_jpeg(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.convert("RGB").save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()


def render_pdf_thumbnail(data: bytes, size: int) -> PdfThumbnail:
    """Render page 1 of a PDF into a `size` px JPEG and measure the document.

    Page sizes are in PDF points with the page's `/Rotate` applied, the
    space pdf shapes use. `text_layer` is true when any page has characters
    (a scan without OCR has none). Raises :class:`ThumbnailError` for input
    PDFium cannot open, including encrypted documents.
    """
    if size < 1:
        raise ValueError("thumbnail size must be positive")
    with PDFIUM_LOCK:
        try:
            document = pdfium.PdfDocument(data)
        except pdfium.PdfiumError as exc:
            raise ThumbnailError(f"cannot open pdf: {exc}") from exc
        try:
            page_count = len(document)
            if page_count == 0:
                raise ThumbnailError("pdf has no pages")
            sizes: list[list[float]] = []
            text_layer = False
            for index in range(page_count):
                if index >= MAX_PAGE_SIZES and text_layer:
                    break
                page = document[index]
                try:
                    if index < MAX_PAGE_SIZES:
                        # PDFium reports the size with `/Rotate` already applied.
                        width, height = page.get_size()
                        sizes.append([round(width, 2), round(height, 2)])
                    if not text_layer:
                        textpage = page.get_textpage()
                        try:
                            text_layer = textpage.count_chars() > 0
                        finally:
                            textpage.close()
                finally:
                    page.close()
            first = document[0]
            try:
                width, height = sizes[0]
                bitmap = first.render(scale=size / max(width, height, 1.0))
                image = bitmap.to_pil()
                bitmap.close()
            finally:
                first.close()
        except pdfium.PdfiumError as exc:
            raise ThumbnailError(f"cannot render pdf: {exc}") from exc
        finally:
            document.close()
    return PdfThumbnail(
        jpeg=_encode_jpeg(image),
        meta={"page_count": page_count, "page_sizes": sizes, "text_layer": text_layer},
    )


async def select_items(
    session: AsyncSession,
    project_id: UUID,
    *,
    force: bool = False,
    item_ids: Sequence[UUID] | None = None,
) -> list[Item]:
    """Image and PDF items still lacking a thumbnail (or all of them, with `force`)."""
    stmt = (
        select(Item)
        .where(
            Item.project_id == project_id,
            Item.media_type.in_((MediaType.IMAGE, MediaType.PDF)),
        )
        .order_by(Item.created_at, Item.id)
    )
    if not force:
        stmt = stmt.where(Item.thumbnail_path.is_(None))
    if item_ids is not None:
        stmt = stmt.where(Item.id.in_(list(item_ids)))
    return list((await session.scalars(stmt)).all())
