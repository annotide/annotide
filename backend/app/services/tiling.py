"""Deep Zoom (DZI) tile pyramids for large image items (IMG-1, IMG-2).

The browser never loads a gigapixel photo whole. The `tile_image` job stages
one item at a time to a local temporary file (streamed in bounded ranged
reads, never a Pillow-style full decode), opens it with libvips in
sequential-access mode, and writes a 256 px JPEG pyramid with `pyvips.dzsave`
(`layout="dz"`, the Deep Zoom convention: level 0 is 1x1 px, the deepest
level is full resolution). The pyramid is *derived data* under
`cache/tiles/{item_id}/` on the project's cache connector (SRC-6), same as a
thumbnail (IMG-8) — safe to delete and regenerate, never a copy of the
customer's file (ARC-3).

`pyvips` calls are blocking C extension calls; every one of them runs through
`asyncio.to_thread` so the worker's event loop keeps reporting progress.
"""

from __future__ import annotations

import asyncio
import math
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

import pyvips
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import StorageConnector
from app.models import Item, MediaType

#: Deep Zoom tile geometry (IMG-1): fixed for every pyramid this job writes.
TILE_SIZE = 256
OVERLAP = 0
TILE_SUFFIX = "jpeg"
TILE_CONTENT_TYPE = "image/jpeg"
DZI_CONTENT_TYPE = "application/xml"
JPEG_QUALITY = 85

#: Range-read chunk while staging a source to a local temp file.
_RANGE_CHUNK_BYTES = 8 * 1024 * 1024
#: Uploads (and the read-from-disk beside them) in flight at once.
_UPLOAD_CONCURRENCY = 16
_DZI_BASENAME = "image"


class TilingError(ValueError):
    """The source bytes are not an image libvips can open (or dzsave failed)."""


def tiles_blob_prefix(item_id: UUID) -> str:
    """Where an item's tile pyramid lives on the cache connector (see *Blob layout*)."""
    return f"cache/tiles/{item_id}/"


def tile_blob_path(item_id: UUID, level: int, col: int, row: int, suffix: str = TILE_SUFFIX) -> str:
    """The blob path of one tile: `cache/tiles/{id}/image_files/{level}/{col}_{row}.{suffix}`."""
    return f"{tiles_blob_prefix(item_id)}image_files/{level}/{col}_{row}.{suffix}"


def max_level_for(width: int, height: int) -> int:
    """`ceil(log2(max(width, height)))`; level 0 is always 1x1 px."""
    longest = max(width, height, 1)
    if longest <= 1:
        return 0
    return math.ceil(math.log2(longest))


def level_dims(width: int, height: int, max_level: int, level: int) -> tuple[int, int]:
    """The pixel size of `level` in a pyramid whose full resolution is `width` x `height`."""
    scale = 2 ** (max_level - level)
    return math.ceil(width / scale), math.ceil(height / scale)


def grid_dims(level_width: int, level_height: int, tile_size: int = TILE_SIZE) -> tuple[int, int]:
    """How many (cols, rows) of `tile_size` tiles cover a level of this pixel size."""
    return math.ceil(level_width / tile_size), math.ceil(level_height / tile_size)


def _meta_int(tiles_meta: dict[str, object], key: str, default: int | None = None) -> int:
    """An integer field of `meta.tiles` (JSONB, so typed `object` here)."""
    value = tiles_meta.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ValueError(f"meta.tiles.{key} is missing or not a number")
    return int(value)


def validate_tile_coordinate(tiles_meta: dict[str, object], level: int, col: int, row: int) -> None:
    """Raise :class:`ValueError` when `(level, col, row)` is outside the pyramid's grid.

    `tiles_meta` is the item's `meta.tiles` dict. The caller (the sign
    endpoint) turns this into a 422.
    """
    max_level = _meta_int(tiles_meta, "max_level")
    width = _meta_int(tiles_meta, "width")
    height = _meta_int(tiles_meta, "height")
    tile_size = _meta_int(tiles_meta, "tile_size", TILE_SIZE)
    if level < 0 or level > max_level:
        raise ValueError(f"level {level} is above max_level {max_level}")
    level_w, level_h = level_dims(width, height, max_level, level)
    cols, rows = grid_dims(level_w, level_h, tile_size)
    if col < 0 or col >= cols or row < 0 or row >= rows:
        raise ValueError(f"({col}, {row}) is outside the {cols}x{rows} grid at level {level}")


async def select_items(
    session: AsyncSession,
    project_id: UUID,
    *,
    force: bool = False,
    item_ids: Sequence[UUID] | None = None,
) -> list[Item]:
    """Image items still lacking `meta.tiles` (or every image item, with `force`)."""
    stmt = (
        select(Item)
        .where(Item.project_id == project_id, Item.media_type == MediaType.IMAGE)
        .order_by(Item.created_at, Item.id)
    )
    if item_ids is not None:
        stmt = stmt.where(Item.id.in_(list(item_ids)))
    items = list((await session.scalars(stmt)).all())
    if force:
        return items
    return [item for item in items if "tiles" not in (item.meta or {})]


async def _stream_to_file(
    storage: StorageConnector, path: str, dest: Path, size_bytes: int
) -> None:
    """Range-read `path` in `_RANGE_CHUNK_BYTES` chunks into `dest`."""
    with dest.open("wb") as handle:
        start = 0
        while start < size_bytes:
            end = min(start + _RANGE_CHUNK_BYTES, size_bytes)
            chunk = await storage.read(path, start=start, end=end)
            if not chunk:
                break
            handle.write(chunk)
            start += len(chunk)


def _open_sequential(path: str) -> pyvips.Image:
    return pyvips.Image.new_from_file(path, access="sequential")


def _dzsave(image: pyvips.Image, base: Path) -> None:
    image.dzsave(
        str(base),
        tile_size=TILE_SIZE,
        overlap=OVERLAP,
        suffix=f".jpeg[Q={JPEG_QUALITY}]",
        layout="dz",
    )


def _render_thumbnail(path: str, size: int) -> bytes:
    thumbnail = pyvips.Image.thumbnail(path, size)
    return bytes(thumbnail.write_to_buffer(f".jpg[Q={JPEG_QUALITY}]"))


async def _upload_pyramid(storage: StorageConnector, base_dir: Path, item_id: UUID) -> None:
    """Upload `image.dzi` and every `image_files/{level}/{col}_{row}.jpeg`, 16 at a time."""
    prefix = tiles_blob_prefix(item_id)
    semaphore = asyncio.Semaphore(_UPLOAD_CONCURRENCY)

    async def _upload(local_path: Path, blob_path: str, content_type: str) -> None:
        async with semaphore:
            data = await asyncio.to_thread(local_path.read_bytes)
            await storage.write(blob_path, data, content_type)

    tasks = [_upload(base_dir / f"{_DZI_BASENAME}.dzi", f"{prefix}image.dzi", DZI_CONTENT_TYPE)]
    files_dir = base_dir / f"{_DZI_BASENAME}_files"
    for level_dir in sorted(files_dir.iterdir(), key=lambda p: p.name):
        if not level_dir.is_dir():
            continue
        for tile_file in level_dir.iterdir():
            blob_path = f"{prefix}image_files/{level_dir.name}/{tile_file.name}"
            tasks.append(_upload(tile_file, blob_path, TILE_CONTENT_TYPE))
    await asyncio.gather(*tasks)


@dataclass(slots=True)
class TileResult:
    """What :func:`tile_item` did for one item."""

    status: Literal["tiled", "skipped_small", "skipped_too_large"]
    width: int | None = None
    height: int | None = None
    meta: dict[str, object] | None = None
    thumbnail: bytes | None = None


async def tile_item(
    *,
    source_storage: StorageConnector,
    cache_storage: StorageConnector,
    item_id: UUID,
    item_path: str,
    item_size_bytes: int,
    work_dir: str | None,
    min_pixels: int,
    max_source_bytes: int,
    thumbnail_size: int,
    tile_size: int = TILE_SIZE,
) -> TileResult:
    """Stage, measure and tile one item.

    Returns a :class:`TileResult` for a source too large or too small to
    tile. Raises :class:`TilingError` for a source libvips cannot open (an
    unsupported whole-slide format, or corrupt bytes) and lets
    :class:`~app.connectors.errors.ConnectorError` from the storage calls
    propagate unchanged, same as the `thumbnail` job. The temporary file and
    pyramid directory are always removed before this returns.
    """
    if item_size_bytes > max_source_bytes:
        return TileResult(status="skipped_too_large")

    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        tmp_path = Path(tmp)
        source_file = tmp_path / "source"
        await _stream_to_file(source_storage, item_path, source_file, item_size_bytes)

        try:
            image = await asyncio.to_thread(_open_sequential, str(source_file))
            width, height = image.width, image.height
            if width * height < min_pixels:
                return TileResult(status="skipped_small", width=width, height=height)

            base = tmp_path / _DZI_BASENAME
            await asyncio.to_thread(_dzsave, image, base)
            thumbnail_bytes = await asyncio.to_thread(
                _render_thumbnail, str(source_file), thumbnail_size
            )
        except pyvips.Error as exc:
            raise TilingError(f"{item_path}: {exc}") from exc

        await _upload_pyramid(cache_storage, tmp_path, item_id)

    max_level = max_level_for(width, height)
    meta: dict[str, object] = {
        "format": "dzi",
        "path": tiles_blob_prefix(item_id),
        "tile_size": tile_size,
        "overlap": OVERLAP,
        "suffix": TILE_SUFFIX,
        "max_level": max_level,
        "width": width,
        "height": height,
    }
    return TileResult(
        status="tiled", width=width, height=height, meta=meta, thumbnail=thumbnail_bytes
    )
