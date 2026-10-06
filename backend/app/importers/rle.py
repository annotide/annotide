"""Run-length-encoded mask decoders shared by the COCO and Label Studio importers.

Two independent RLE dialects are supported, both decoded to the same
representation: an uncompressed, column-major list of alternating
background/foreground run lengths (COCO's own uncompressed `counts`, see
`app.schemas.annotation.MaskRLE`).

- `decode_coco_compressed`: pycocotools' LEB128-style string encoding
  (`counts` a `str`). Mirrors `rleFrString` in pycocotools' `maskApi.c`.
- `decode_label_studio_brush`: Label Studio's `brushlabels` bit-packed RLE
  (`format: "rle"`). Mirrors `decode_rle` in label-studio-converter's
  `brush.py`; the decoded buffer is a row-major RGBA byte array, and the
  mask is its alpha channel thresholded at zero.

Both raise `ValueError` on malformed input; callers turn that into an
import warning and skip the shape, matching the style of the rest of
`app.importers`.
"""

from __future__ import annotations


def decode_coco_compressed(counts: str) -> list[int]:
    """Decode a pycocotools compressed RLE `counts` string.

    Returns the equivalent uncompressed, column-major run lengths (COCO's
    own `counts` list when `segmentation.counts` is a list rather than a
    string).
    """
    decoded: list[int] = []
    length = len(counts)
    position = 0
    while position < length:
        value = 0
        shift = 0
        more = True
        while more:
            if position >= length:
                raise ValueError("truncated compressed RLE string")
            char_code = ord(counts[position]) - 48
            if char_code < 0:
                raise ValueError("invalid compressed RLE character")
            value |= (char_code & 0x1F) << (5 * shift)
            more = bool(char_code & 0x20)
            position += 1
            shift += 1
            if not more and (char_code & 0x10):
                value |= -1 << (5 * shift)
        if len(decoded) > 2:
            value += decoded[-2]
        decoded.append(value)
    return decoded


class _BitReader:
    """Reads big-endian bit fields out of a byte sequence, MSB first."""

    __slots__ = ("_bits", "_position")

    def __init__(self, data: list[int]) -> None:
        self._bits = "".join(f"{byte & 0xFF:08b}" for byte in data)
        self._position = 0

    def read(self, size: int) -> int:
        end = self._position + size
        if end > len(self._bits):
            raise ValueError("truncated brush RLE bitstream")
        chunk = self._bits[self._position : end]
        self._position = end
        return int(chunk, 2)


def _mask_to_coco_counts(mask: list[int], width: int, height: int) -> list[int]:
    """Convert a row-major binary mask to COCO uncompressed column-major counts."""
    counts: list[int] = []
    current_value = 0
    run_length = 0
    for column in range(width):
        for row in range(height):
            value = mask[row * width + column]
            if value == current_value:
                run_length += 1
            else:
                counts.append(run_length)
                current_value = value
                run_length = 1
    counts.append(run_length)
    return counts


def decode_label_studio_brush(rle: list[int], width: int, height: int) -> list[int]:
    """Decode a Label Studio `brushlabels` bit-packed RLE (`format: "rle"`).

    `rle` is the raw list of bytes (0-255) as exported by Label Studio.
    Returns the equivalent uncompressed, column-major COCO `counts`.
    """
    if not rle:
        raise ValueError("empty brush RLE")

    reader = _BitReader(rle)
    num_values = reader.read(32)
    word_size = reader.read(5) + 1
    rle_sizes = [reader.read(4) + 1 for _ in range(4)]

    if num_values != width * height * 4:
        raise ValueError("brush RLE length does not match width*height*4")

    values = [0] * num_values
    index = 0
    while index < num_values:
        span = min(num_values - index, 8)
        if reader.read(1):
            value = reader.read(word_size)
            for _ in range(span):
                values[index] = value
                index += 1
        else:
            for _ in range(span):
                size = rle_sizes[reader.read(2)]
                values[index] = reader.read(size)
                index += 1

    # The decoded buffer is a flat, row-major RGBA byte array; the mask is
    # its alpha channel (every 4th byte) thresholded at zero.
    mask = [1 if values[pixel * 4 + 3] > 0 else 0 for pixel in range(width * height)]
    return _mask_to_coco_counts(mask, width, height)
