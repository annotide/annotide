"""Derived-data cache: where `cache/` lives and how it is rebuilt (SRC-6).

Thumbnails (IMG-8) and tile pyramids (IMG-1) are *derived data*: generated
from the customer's media, safe to delete, and written to the project's
effective cache connector (`project.cache_connector_id`, else the result
connector) so they can sit in their own container with a lifecycle policy.

A rebuild forgets every pointer into the cache (`item.thumbnail_path`,
`item.meta.tiles`) and lets the `thumbnail` and `tile_image` jobs write them
again. Purging is per item — `cache/` is keyed by item id, and a container
may be shared with other projects, so the prefix as a whole is never deleted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.base import StorageConnector
from app.connectors.errors import ConnectorError, ConnectorNotFound
from app.models import Item, MediaType
from app.services.thumbnails import thumbnail_blob_path
from app.services.tiling import tiles_blob_prefix


@dataclass(slots=True)
class ClearedCache:
    """What :func:`clear_project_cache` forgot."""

    thumbnails: int = 0
    #: Items that had `meta.tiles`, in creation order: the ones to re-tile.
    tiled_item_ids: list[UUID] = field(default_factory=list)
    #: Every image / PDF item of the project, for a per-item purge.
    item_ids: list[UUID] = field(default_factory=list)


async def clear_project_cache(session: AsyncSession, project_id: UUID) -> ClearedCache:
    """Drop `thumbnail_path` and `meta.tiles` from the project's items; the caller commits."""
    items = await session.scalars(
        select(Item)
        .where(
            Item.project_id == project_id,
            Item.media_type.in_((MediaType.IMAGE, MediaType.PDF)),
        )
        .order_by(Item.created_at, Item.id)
    )
    cleared = ClearedCache()
    for item in items:
        cleared.item_ids.append(item.id)
        if item.thumbnail_path is not None:
            item.thumbnail_path = None
            cleared.thumbnails += 1
        if "tiles" in (item.meta or {}):
            # A new dict, so the JSONB change is detected.
            item.meta = {key: value for key, value in item.meta.items() if key != "tiles"}
            cleared.tiled_item_ids.append(item.id)
    return cleared


async def purge_item_cache(storage: StorageConnector, item_id: UUID) -> tuple[int, list[str]]:
    """Delete one item's thumbnail and tile pyramid; returns (blobs deleted, errors).

    Best effort: a blob that is already gone is skipped, one that cannot be
    deleted is reported, never fatal — the rebuild overwrites the same paths.
    """
    deleted = 0
    errors: list[str] = []
    paths = [thumbnail_blob_path(item_id)]
    try:
        paths.extend([info.path async for info in storage.list(tiles_blob_prefix(item_id))])
    except ConnectorError as exc:
        errors.append(f"{tiles_blob_prefix(item_id)}: {exc}")
    for path in paths:
        try:
            await storage.delete(path)
        except ConnectorNotFound:
            continue
        except ConnectorError as exc:
            errors.append(f"{path}: {exc}")
            continue
        deleted += 1
    return deleted, errors
