"""Read image dimensions from a file header (SRC-5).

The annotator needs an item's pixel width and height before it can place a
single shape — every coordinate in an ``AnnotationResult`` is relative to them.

Downloading whole images to measure them would defeat SRC-5 ("large files are
read with range requests; files are never copied to the platform"). A few
hundred bytes of header is enough for every format below, so a scan of a
million-image container transfers megabytes rather than terabytes.

Returns ``None`` rather than raising for anything unrecognised: an item whose
dimensions we cannot read is still a perfectly valid item, it just cannot be
opened in the image annotator yet.
"""

from __future__ import annotations

import struct

#: Enough for a PNG IHDR, a GIF header, a BMP header, a WebP VP8 chunk, and
#: the early segments of most JPEGs. JPEGs that carry a large EXIF thumbnail
#: before their SOF marker need more, hence the larger cap below.
HEADER_BYTES = 2048
JPEG_SCAN_LIMIT = 256 * 1024


def image_size(header: bytes) -> tuple[int, int] | None:
    """Best-effort (width, height) from the first bytes of an image file."""
    for reader in (_png_size, _jpeg_size, _gif_size, _bmp_size, _webp_size):
        size = reader(header)
        if size is not None:
            return size
    return None


def _png_size(data: bytes) -> tuple[int, int] | None:
    # 8-byte signature, then an IHDR chunk whose first two fields are the
    # dimensions as big-endian uint32.
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    if data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    # Walk the segment chain to the start-of-frame marker, which carries the
    # dimensions. Everything before it (EXIF, ICC, thumbnails) is skipped by
    # its declared length rather than scanned byte by byte.
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None

    offset = 2
    limit = min(len(data), JPEG_SCAN_LIMIT)
    while offset + 9 < limit:
        if data[offset] != 0xFF:
            offset += 1
            continue

        marker = data[offset + 1]
        # Standalone markers carry no payload.
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        # Start of scan: image data begins, no dimensions past this point.
        if marker == 0xDA:
            return None

        (segment_length,) = struct.unpack(">H", data[offset + 2 : offset + 4])
        # SOF0..SOF15, excluding the DHT/JPG/DAC markers interleaved in that range.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[offset + 5 : offset + 9])
            return int(width), int(height)

        if segment_length < 2:
            return None
        offset += 2 + segment_length

    return None


def _gif_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    width, height = struct.unpack("<HH", data[6:10])
    return int(width), int(height)


def _bmp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 26 or data[:2] != b"BM":
        return None
    width, height = struct.unpack("<ii", data[18:26])
    # A negative height means a top-down bitmap; the magnitude is the size.
    return int(abs(width)), int(abs(height))


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None

    chunk = data[12:16]
    if chunk == b"VP8 ":
        width, height = struct.unpack("<HH", data[26:30])
        return int(width & 0x3FFF), int(height & 0x3FFF)
    if chunk == b"VP8L":
        (bits,) = struct.unpack("<I", data[21:25])
        return int((bits & 0x3FFF) + 1), int(((bits >> 14) & 0x3FFF) + 1)
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    return None
