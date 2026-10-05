"""Tests for app.importers.rle: COCO compressed and Label Studio brush RLE decoding (EXP-6)."""

from __future__ import annotations

import pytest

from app.importers.rle import decode_coco_compressed, decode_label_studio_brush

# ---------------------------------------------------------------------------
# helpers: reference encoders, mirroring the decoders under test
# ---------------------------------------------------------------------------


def _encode_coco_compressed(counts: list[int]) -> str:
    """Mirrors pycocotools' `rleToString` (maskApi.c) for round-trip tests."""
    chars: list[str] = []
    for i, count in enumerate(counts):
        x = count
        if i > 2:
            x -= counts[i - 2]
        more = True
        while more:
            char_code = x & 0x1F
            x >>= 5
            more = x != -1 if char_code & 0x10 else x != 0
            if more:
                char_code |= 0x20
            chars.append(chr(char_code + 48))
    return "".join(chars)


class _BitWriter:
    """Mirrors the bit layout `decode_label_studio_brush` reads: MSB first."""

    def __init__(self) -> None:
        self._bits: list[str] = []

    def write(self, value: int, size: int) -> None:
        self._bits.append(format(value, f"0{size}b"))

    def to_bytes(self) -> list[int]:
        bitstring = "".join(self._bits)
        bitstring += "0" * ((-len(bitstring)) % 8)
        return [int(bitstring[i : i + 8], 2) for i in range(0, len(bitstring), 8)]


def _encode_label_studio_brush(groups_of_8: list[int]) -> list[int]:
    """Encode a brush RLE using only constant-span (flag=1) groups of 8 bytes.

    `groups_of_8` gives one byte value per 8-byte span of the flat RGBA
    buffer; the buffer length is therefore `len(groups_of_8) * 8`.
    """
    num_values = len(groups_of_8) * 8
    writer = _BitWriter()
    writer.write(num_values, 32)
    writer.write(7, 5)  # word_size - 1 = 7 -> word_size = 8
    for _ in range(4):
        writer.write(0, 4)  # rle_sizes, unused by constant-span groups
    for value in groups_of_8:
        writer.write(1, 1)  # flag: constant value for this span
        writer.write(value, 8)
    return writer.to_bytes()


# ---------------------------------------------------------------------------
# decode_coco_compressed
# ---------------------------------------------------------------------------


def test_decode_coco_compressed_known_vector() -> None:
    # A 100x100 mask: 100 background, 50 foreground, 9850 background.
    assert decode_coco_compressed("T3b1jc9") == [100, 50, 9850]


@pytest.mark.parametrize(
    "counts",
    [
        [0, 10000],
        [10, 5, 85],
        [4, 4],
        [0, 1, 1, 0, 1, 2, 3, 4, 5, 100, 200, 300],
        [1] * 20,
    ],
)
def test_decode_coco_compressed_round_trip(counts: list[int]) -> None:
    assert decode_coco_compressed(_encode_coco_compressed(counts)) == counts


def test_decode_coco_compressed_truncated_raises() -> None:
    # 'P' - 48 = 0x20: continuation bit set with no following byte.
    with pytest.raises(ValueError, match="truncated"):
        decode_coco_compressed("P")


def test_decode_coco_compressed_invalid_character_raises() -> None:
    with pytest.raises(ValueError, match="invalid"):
        decode_coco_compressed("\x00")


# ---------------------------------------------------------------------------
# decode_label_studio_brush
# ---------------------------------------------------------------------------


def test_decode_label_studio_brush_known_vector() -> None:
    # width=4, height=2; alpha pattern (row-major) is [0,0,1,1, 0,0,1,1].
    rle = _encode_label_studio_brush([0, 255, 0, 255])
    assert decode_label_studio_brush(rle, width=4, height=2) == [4, 4]


def test_decode_label_studio_brush_all_foreground() -> None:
    rle = _encode_label_studio_brush([255, 255])
    counts = decode_label_studio_brush(rle, width=2, height=2)
    assert counts == [0, 4]
    assert sum(counts) == 4


def test_decode_label_studio_brush_all_background() -> None:
    rle = _encode_label_studio_brush([0, 0])
    counts = decode_label_studio_brush(rle, width=2, height=2)
    assert counts == [4]
    assert sum(counts) == 4


def test_decode_label_studio_brush_empty_raises() -> None:
    with pytest.raises(ValueError, match="empty"):
        decode_label_studio_brush([], width=4, height=2)


def test_decode_label_studio_brush_size_mismatch_raises() -> None:
    rle = _encode_label_studio_brush([0, 255, 0, 255])
    with pytest.raises(ValueError, match="width\\*height"):
        decode_label_studio_brush(rle, width=4, height=4)


def test_decode_label_studio_brush_truncated_raises() -> None:
    with pytest.raises(ValueError, match="truncated"):
        decode_label_studio_brush([0, 0], width=4, height=2)
