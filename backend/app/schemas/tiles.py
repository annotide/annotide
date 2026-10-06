"""Request/response DTOs for large-image tiling (IMG-1, IMG-2).

Imported directly (`from app.schemas.tiles import ...`), not re-exported from
`app.schemas`, per the split-work note in this unit's task: these DTOs are
used by exactly the `tiles` job-queue route and the per-item tile-sign route.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field

from app.schemas.common import BaseSchema


class TileJobRequest(BaseSchema):
    """Body for `POST /projects/{id}/tiles` (IMG-1)."""

    #: Re-tile items that already have `meta.tiles`.
    force: bool = False
    #: Restrict the run to these items; default is every image item.
    item_ids: list[UUID] | None = Field(default=None, max_length=10_000)


class TileSignRequest(BaseSchema):
    """Body for `POST /items/{id}/tiles/sign`: `[level, col, row]` triples."""

    tiles: list[tuple[int, int, int]] = Field(min_length=1, max_length=512)


class TileSignResponse(BaseSchema):
    """Signed URLs for the requested tiles, in the same order as the request."""

    urls: list[str]
    expires_in: int
