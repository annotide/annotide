"""Small fakes shared by several test modules."""

from __future__ import annotations

import struct
import zlib
from uuid import UUID

from app.models import Job


def png(width: int, height: int, pixels: bytearray) -> bytes:
    """Encode raw RGB bytes as a PNG without an image library.

    Enough for tests that need a file the scanner will accept and measure:
    each scanline is prefixed with a zero filter byte, the lot is deflated,
    and three chunks wrap it.
    """
    raw = bytearray()
    stride = width * 3
    for row in range(height):
        raw.append(0)
        raw.extend(pixels[row * stride : (row + 1) * stride])

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


class FakeJobQueue:
    """Records what the API enqueues or aborts instead of talking to Redis.

    Set `fail_with` to make every call raise, to exercise the 503 path.
    """

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.enqueued: list[tuple[UUID, str]] = []
        self.aborted: list[UUID] = []
        self.requeued: list[UUID] = []
        #: Job ids the fake "Redis" still holds, for stranded-job tests.
        self.known: set[UUID] = set()
        self.fail_with = fail_with

    async def enqueue(self, job: Job) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.enqueued.append((job.id, job.type.value))

    async def requeue(self, job: Job) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.requeued.append(job.id)
        self.enqueued.append((job.id, job.type.value))

    async def abort(self, job_id: UUID) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.aborted.append(job_id)

    async def exists(self, job_id: UUID) -> bool:
        if self.fail_with is not None:
            raise self.fail_with
        return job_id in self.known

    async def aclose(self) -> None:
        return None


def pdf(pages: list[str], *, rotate: dict[int, int] | None = None) -> bytes:
    """A minimal PDF, one 600 x 800 pt page per entry, with that text in Helvetica.

    An empty string makes a page without a text layer. `rotate` maps a
    0-based page index to its `/Rotate`.
    """
    rotate = rotate or {}
    objects: dict[int, str] = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        3: "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    kids = []
    for index, text in enumerate(pages):
        page_id, content_id = 4 + 2 * index, 5 + 2 * index
        stream = f"BT /F1 12 Tf 100 700 Td ({text}) Tj ET" if text else ""
        objects[content_id] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
        extra = f" /Rotate {rotate[index]}" if index in rotate else ""
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
