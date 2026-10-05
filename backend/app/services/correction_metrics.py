"""How much humans changed a model version's pre-labels (ML-5).

For every item a model version pre-labelled, the *final* human version of
that item (the latest `submitted` / `approved` version written by a person
after the draft) is compared with the draft shape by shape. Shape ids are
stable across versions — the annotator edits the draft rather than starting
over — so a shape present on both sides with the same geometry and class was
*kept*, with a different class *relabeled*, with different geometry
*adjusted*; a shape only on the model side was *deleted*, only on the human
side *added*. From those, precision (of what the model drew, how much
survived) and recall (of what the human ended up with, how much the model
had drawn) per class, and the mean IoU of adjusted boxes. Items nobody has
finished yet count as *pending* and take no part in the numbers.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Annotation, AnnotationKind, AnnotationSource, AnnotationStatus, Item
from app.schemas import AnnotationResult
from app.schemas.annotation import BBoxShape
from app.schemas.model import CorrectionClassMetrics, CorrectionMetrics, CorrectionShapeCounts

#: Human statuses that count as a finished correction.
_FINAL = (AnnotationStatus.SUBMITTED, AnnotationStatus.APPROVED)


@dataclass
class _Tally:
    model: int = 0
    kept: int = 0
    adjusted: int = 0
    relabeled: int = 0
    deleted: int = 0
    added: int = 0
    #: Shapes relabeled *into* this class from another; they are final shapes
    #: of this class the model did not draw as such (recall denominator only).
    relabeled_in: int = 0
    ious: list[float] = field(default_factory=list)

    def counts(self) -> CorrectionShapeCounts:
        return CorrectionShapeCounts(
            model=self.model,
            kept=self.kept,
            adjusted=self.adjusted,
            relabeled=self.relabeled,
            deleted=self.deleted,
            added=self.added,
        )

    @property
    def precision(self) -> float | None:
        # A model shape "survived" when the human kept or only adjusted it;
        # a relabel means the model saw something but called it wrong.
        return None if self.model == 0 else round((self.kept + self.adjusted) / self.model, 4)

    @property
    def recall(self) -> float | None:
        # Everything the human ended up with in this class.
        final = self.kept + self.adjusted + self.added + self.relabeled_in
        return None if final == 0 else round((self.kept + self.adjusted) / final, 4)

    @property
    def mean_iou(self) -> float | None:
        return None if not self.ious else round(sum(self.ious) / len(self.ious), 4)


def bbox_iou(a: BBoxShape, b: BBoxShape) -> float:
    """Intersection over union of two axis-aligned boxes."""
    ax0, ay0, ax1, ay1 = a.bbox
    bx0, by0, bx1, by1 = b.bbox
    inter_w = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    inter_h = max(0.0, min(ay1, by1) - max(ay0, by0))
    inter = inter_w * inter_h
    union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return 0.0 if union <= 0 else inter / union


def _geometry(shape: Any) -> Any:
    """The shape without its class / attributes / confidence, for comparison."""
    return shape.model_dump(exclude={"class_", "attributes", "confidence"}, by_alias=True)


def compare_shapes(
    draft: AnnotationResult, final: AnnotationResult, tallies: dict[str, _Tally]
) -> bool:
    """Tally one item's draft against its final version. Returns True when
    the human changed nothing."""
    draft_by_id = {shape.id: shape for shape in draft.shapes}
    final_by_id = {shape.id: shape for shape in final.shapes}
    unchanged = True
    for shape_id, before in draft_by_id.items():
        tally = tallies[before.class_]
        tally.model += 1
        after = final_by_id.get(shape_id)
        if after is None:
            tally.deleted += 1
            unchanged = False
        elif after.class_ != before.class_:
            tally.relabeled += 1
            tallies[after.class_].relabeled_in += 1
            unchanged = False
        elif _geometry(after) != _geometry(before):
            tally.adjusted += 1
            unchanged = False
            if isinstance(before, BBoxShape) and isinstance(after, BBoxShape):
                tally.ious.append(bbox_iou(before, after))
        else:
            tally.kept += 1
    for shape_id, after in final_by_id.items():
        if shape_id not in draft_by_id:
            tallies[after.class_].added += 1
            unchanged = False
    return unchanged


async def correction_metrics(
    session: AsyncSession, *, version_id: UUID, project_id: UUID | None = None
) -> CorrectionMetrics:
    """Compare every draft by `version_id` (optionally within `project_id`)
    with the item's final human version."""
    draft_stmt = select(Annotation).where(Annotation.author_model_version_id == version_id)
    if project_id is not None:
        draft_stmt = draft_stmt.join(Item, Item.id == Annotation.item_id).where(
            Item.project_id == project_id
        )
    drafts = list(await session.scalars(draft_stmt))
    # One draft per item per version (the job is idempotent); keep the latest if not.
    draft_by_item: dict[UUID, Annotation] = {}
    for draft in drafts:
        current = draft_by_item.get(draft.item_id)
        if current is None or draft.version > current.version:
            draft_by_item[draft.item_id] = draft

    finals: dict[UUID, Annotation] = {}
    if draft_by_item:
        rows = await session.scalars(
            select(Annotation).where(
                Annotation.item_id.in_(list(draft_by_item)),
                Annotation.source == AnnotationSource.HUMAN,
                Annotation.status.in_(_FINAL),
                Annotation.kind == AnnotationKind.PRIMARY,
            )
        )
        for row in rows:
            if row.version <= draft_by_item[row.item_id].version:
                continue
            current = finals.get(row.item_id)
            if current is None or row.version > current.version:
                finals[row.item_id] = row

    tallies: dict[str, _Tally] = defaultdict(_Tally)
    accepted = 0
    for item_id, draft in draft_by_item.items():
        final = finals.get(item_id)
        if final is None:
            continue
        if compare_shapes(
            AnnotationResult.model_validate(draft.result),
            AnnotationResult.model_validate(final.result),
            tallies,
        ):
            accepted += 1

    total = _Tally()
    for tally in tallies.values():
        total.model += tally.model
        total.kept += tally.kept
        total.adjusted += tally.adjusted
        total.relabeled += tally.relabeled
        total.deleted += tally.deleted
        total.added += tally.added
        total.relabeled_in += tally.relabeled_in
        total.ious.extend(tally.ious)

    return CorrectionMetrics(
        model_version_id=version_id,
        project_id=project_id,
        items_predicted=len(draft_by_item),
        items_corrected=len(finals),
        items_pending=len(draft_by_item) - len(finals),
        items_accepted_unchanged=accepted,
        shapes=total.counts(),
        precision=total.precision,
        recall=total.recall,
        mean_iou_adjusted=total.mean_iou,
        classes=sorted(
            (
                CorrectionClassMetrics(
                    name=name,
                    **tally.counts().model_dump(),
                    precision=tally.precision,
                    recall=tally.recall,
                    mean_iou_adjusted=tally.mean_iou,
                )
                for name, tally in tallies.items()
            ),
            key=lambda row: (-(row.model + row.added), row.name),
        ),
    )
