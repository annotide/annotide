"""Request/response DTOs for `POST /items/{id}/split` (IMG-6).

Imported directly (`from app.schemas.split import ...`), not re-exported from
`app.schemas`, per the split-work note in this unit's task: it is a small,
self-contained pair of DTOs used by exactly one router.
"""

from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from app.schemas.common import BaseSchema
from app.schemas.task import TaskRead


class SplitGrid(BaseSchema):
    """Split an image into `rows` x `cols` equal cells, 1-16 each.

    Cells extend by `overlap_px` on their *inner* edges only (an edge cell's
    outer border stays at the image edge), then are clipped to the image.
    """

    rows: int = Field(ge=1, le=16)
    cols: int = Field(ge=1, le=16)
    overlap_px: float = Field(default=0, ge=0)


class SplitRequest(BaseSchema):
    """Body of `POST /items/{id}/split`: exactly one of `grid` or `regions`."""

    model_config = ConfigDict(extra="forbid")

    grid: SplitGrid | None = None
    #: `[x_min, y_min, x_max, y_max]` in original-image pixels, 1-256 boxes.
    regions: list[tuple[float, float, float, float]] | None = Field(
        default=None, min_length=1, max_length=256
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> SplitRequest:
        if (self.grid is None) == (self.regions is None):
            raise ValueError("exactly one of 'grid' or 'regions' is required")
        return self


class SplitResponse(BaseSchema):
    """One region task per region opened by the split."""

    tasks: list[TaskRead]
