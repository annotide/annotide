"""Split an image item into region tasks, and merge saves under one (IMG-6).

Two halves of the same feature live here: `split_item` (`POST
/items/{id}/split`) turns an image into a grid or an explicit list of
regions, cancels the item's live ordinary annotate task and opens one region
task per region; `merge_region_result` is what a save under a region task
runs through before it becomes the item's next `primary` version (see
CONTRACTS.md *task*, "Region").
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ConflictError, ValidationFailedError
from app.models import Item, ItemStatus, MediaType, Task, TaskStatus, TaskType
from app.schemas import AnnotationResult
from app.schemas.annotation import (
    BBoxShape,
    KeypointsShape,
    MaskShape,
    PointShape,
    PolygonShape,
    PolylineShape,
    RBoxShape,
    ShapeBase,
)
from app.schemas.project import WorkflowConfig
from app.schemas.split import SplitGrid, SplitRequest
from app.services.annotations import latest_version
from app.services.tasks import open_task

Region = tuple[float, float, float, float]


def _clip(region: Region, width: float, height: float) -> Region | None:
    """Clip `region` to `[0, width] x [0, height]`; `None` when empty afterwards."""
    x_min, y_min, x_max, y_max = region
    x_min = max(0.0, min(x_min, width))
    y_min = max(0.0, min(y_min, height))
    x_max = max(0.0, min(x_max, width))
    y_max = max(0.0, min(y_max, height))
    if x_max <= x_min or y_max <= y_min:
        return None
    return (x_min, y_min, x_max, y_max)


def grid_regions(width: int, height: int, grid: SplitGrid) -> list[Region]:
    """The `rows` x `cols` cells of `grid` over a `width` x `height` image.

    Row-major order (row 0 left-to-right, then row 1, ...). Each cell extends
    by `overlap_px` on its inner edges only, then is clipped to the image.
    """
    cell_w = width / grid.cols
    cell_h = height / grid.rows
    regions: list[Region] = []
    for row in range(grid.rows):
        for col in range(grid.cols):
            x_min = col * cell_w - (grid.overlap_px if col > 0 else 0)
            x_max = (col + 1) * cell_w + (grid.overlap_px if col < grid.cols - 1 else 0)
            y_min = row * cell_h - (grid.overlap_px if row > 0 else 0)
            y_max = (row + 1) * cell_h + (grid.overlap_px if row < grid.rows - 1 else 0)
            regions.append((x_min, y_min, x_max, y_max))
    return regions


#: Statuses an item may be split from (CONTRACTS.md REST `/items/{id}/split`).
_SPLITTABLE = frozenset(
    {ItemStatus.NEW, ItemStatus.PRELABELED, ItemStatus.ANNOTATING, ItemStatus.REJECTED}
)


async def split_item(
    session: AsyncSession,
    *,
    item: Item,
    config: WorkflowConfig,
    request: SplitRequest,
) -> list[Task]:
    """Split `item` into region annotate tasks. Does not commit.

    409s: not an image or no dimensions; status not splittable; an
    in-progress annotate task; live region or consensus tasks already;
    `consensus_annotators` > 1. 422: a region (grid cell or explicit box) is
    empty after clipping to the image.
    """
    if item.media_type is not MediaType.IMAGE or item.width is None or item.height is None:
        raise ConflictError("Only an image item with known width/height can be split.")
    if item.status not in _SPLITTABLE:
        raise ConflictError(f"Item status {item.status.value!r} cannot be split.")
    if config.consensus_annotators > 1:
        raise ConflictError("A project with consensus_annotators > 1 cannot split items.")

    live = list(
        await session.scalars(
            select(Task).where(
                Task.item_id == item.id,
                Task.type == TaskType.ANNOTATE,
                Task.status.in_((TaskStatus.OPEN, TaskStatus.IN_PROGRESS)),
            )
        )
    )
    if any(task.status is TaskStatus.IN_PROGRESS for task in live):
        raise ConflictError("The item has an in-progress annotate task.")
    if any(task.region is not None for task in live):
        raise ConflictError("The item already has live region tasks.")
    if any(task.slot is not None for task in live):
        raise ConflictError("The item has live consensus tasks.")
    ordinary = [task for task in live if task.region is None and task.slot is None]

    if request.grid is not None:
        raw_regions = grid_regions(item.width, item.height, request.grid)
    else:
        assert request.regions is not None
        raw_regions = list(request.regions)

    clipped: list[Region] = []
    for region in raw_regions:
        box = _clip(region, item.width, item.height)
        if box is None:
            raise ValidationFailedError("A region is empty after clipping to the image.")
        clipped.append(box)

    # The live ordinary annotate task is cancelled; region tasks inherit its
    # priority / deadline so the item keeps its place in the queue (WF-6).
    priority = 0
    deadline = None
    for task in ordinary:
        task.status = TaskStatus.CANCELLED
        task.locked_by_id = None
        task.locked_until = None
        task.assignee_id = None
        priority = task.priority
        deadline = task.deadline

    opened: list[Task] = []
    for region in clipped:
        region_task = await open_task(
            session,
            item_id=item.id,
            project_id=item.project_id,
            task_type=TaskType.ANNOTATE,
            priority=priority,
            deadline=deadline,
            region=list(region),
        )
        if region_task is not None:
            opened.append(region_task)
    return opened


def _envelope_centre(points: list[tuple[float, float]]) -> tuple[float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)


def _mask_bbox_centre(rle_size: tuple[int, int], counts: list[int]) -> tuple[float, float] | None:
    """Centre of the axis-aligned envelope of a mask's foreground pixels.

    `counts` is uncompressed, column-major COCO RLE (CONTRACTS.md *import*):
    pixel index `i` sits at column `i // height`, row `i % height`.
    """
    height, _width = rle_size
    min_col = min_row = None
    max_col = max_row = 0.0
    index = 0
    foreground = False
    for run in counts:
        if foreground and run:
            for pixel in range(index, index + run):
                col, row = divmod(pixel, height)
                if min_col is None or col < min_col:
                    min_col = col
                if col > max_col:
                    max_col = col
                if min_row is None or row < min_row:
                    min_row = row
                if row > max_row:
                    max_row = row
        index += run
        foreground = not foreground
    if min_col is None or min_row is None:
        return None
    return ((min_col + max_col) / 2, (min_row + max_row) / 2)


def shape_anchor(shape: ShapeBase) -> tuple[float, float] | None:
    """The anchor CONTRACTS.md defines for a region merge: centre of the
    axis-aligned envelope (bbox, rbox, polygon, polyline, mask, keypoints), the point
    itself (point). `None` for shape types the region split does not apply
    to (span, relation: text-only / referential, never produced on images)."""
    if isinstance(shape, BBoxShape):
        x_min, y_min, x_max, y_max = shape.bbox
        return ((x_min + x_max) / 2, (y_min + y_max) / 2)
    if isinstance(shape, RBoxShape):
        return _envelope_centre(shape.corners())
    if isinstance(shape, PolygonShape | PolylineShape):
        return _envelope_centre(shape.points)
    if isinstance(shape, PointShape):
        return shape.point
    if isinstance(shape, MaskShape):
        return _mask_bbox_centre(shape.rle.size, shape.rle.counts)
    if isinstance(shape, KeypointsShape):
        return _envelope_centre(shape.labelled())
    return None


def _anchor_in_region(shape: ShapeBase, region: Region) -> bool:
    anchor = shape_anchor(shape)
    if anchor is None:
        return True
    x_min, y_min, x_max, y_max = region
    x, y = anchor
    # Half-open: `[x_min, x_max) x [y_min, y_max)` (CONTRACTS.md *task*).
    return x_min <= x < x_max and y_min <= y < y_max


async def merge_region_result(
    session: AsyncSession,
    *,
    item_id: UUID,
    region: Region,
    submitted: AnnotationResult,
) -> AnnotationResult:
    """Server-side merge for a save under a region task (IMG-6).

    Starts from the item's latest `primary` version (empty when none), drops
    its shapes whose anchor lies inside `region`, adds the submitted shapes,
    and overlays the submitted `classification` keys. A submitted shape whose
    anchor lies outside `region` is a 422. Row-locks the item first so two
    concurrent region saves serialise rather than clobber each other.
    """
    out_of_region = [
        str(shape.id) for shape in submitted.shapes if not _anchor_in_region(shape, region)
    ]
    if out_of_region:
        raise ValidationFailedError(
            "Submitted shapes must lie inside the task's region.",
            extra={"shape_ids": out_of_region},
        )

    # Harmless on SQLite (tests); on Postgres this serialises concurrent
    # region saves on the same item.
    await session.execute(select(Item.id).where(Item.id == item_id).with_for_update())

    base = await latest_version(session, item_id)
    if base is not None:
        base_result = AnnotationResult.model_validate(base.result)
        kept_shapes = [
            shape for shape in base_result.shapes if not _anchor_in_region(shape, region)
        ]
        classification = dict(base_result.classification)
        schema_version = base_result.schema_version
        media_type = base_result.media_type
    else:
        kept_shapes = []
        classification = {}
        schema_version = submitted.schema_version
        media_type = submitted.media_type

    classification.update(submitted.classification)

    return AnnotationResult(
        schema_version=schema_version,
        media_type=media_type,
        classification=classification,
        shapes=[*kept_shapes, *submitted.shapes],
    )


__all__ = [
    "grid_regions",
    "merge_region_result",
    "shape_anchor",
    "split_item",
]
