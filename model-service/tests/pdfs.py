"""A minimal PDF writer for tests (same layout as backend/tests/support.py::pdf)."""

from __future__ import annotations


def pdf(
    pages: list[str],
    *,
    rotate: dict[int, int] | None = None,
    crop: tuple[int, int, int, int] | None = None,
) -> bytes:
    """One 600 x 800 pt page per entry, that text in 12 pt Helvetica at (100, 700)."""
    rotate = rotate or {}
    objects: dict[int, str] = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    kids = []
    for index, text in enumerate(pages):
        page_id, content_id = 4 + 2 * index, 5 + 2 * index
        # "\n" in `text` starts a new 14 pt-lower line.
        lines = "".join(f"({line}) Tj 0 -14 Td " for line in text.split("\n"))
        stream = f"BT /F1 12 Tf 100 700 Td {lines}ET" if text else ""
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


def scan(pages: list[str], *, dpi: int = 288) -> bytes:
    """`pdf(pages)` printed and scanned: image-only pages, no text layer, same size."""
    import io

    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf(pages))
    images = []
    try:
        for index in range(len(document)):
            page = document[index]
            bitmap = page.render(scale=dpi / 72)
            images.append(bitmap.to_pil().convert("RGB"))
            bitmap.close()
            page.close()
    finally:
        document.close()
    buffer = io.BytesIO()
    images[0].save(
        buffer, format="PDF", resolution=float(dpi), save_all=True, append_images=images[1:]
    )
    return buffer.getvalue()
