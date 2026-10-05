"""DTOs for quality control (QA-1 … QA-4): consensus, agreement, fusion, gold.

See CONTRACTS.md *Quality control* and the REST rows for `/items/{id}/consensus`,
`/items/{id}/consensus/resolve`, `/items/{id}/gold`, `/projects/{id}/gold/tasks`,
`/projects/{id}/agreement` and `/projects/{id}/quality/annotators`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field

from app.schemas.annotation import AnnotationResult
from app.schemas.common import BaseSchema


class AgreementAnnotator(BaseSchema):
    """One annotator's footprint in a `ProjectAgreement` (how many items they touched)."""

    user_id: UUID
    email: str
    display_name: str
    items: int


class ClassificationAgreement(BaseSchema):
    """Agreement for one classification field, pooled over items with a value (QA-2)."""

    field: str
    items: int
    fleiss_kappa: float | None
    krippendorff_alpha: float | None


class ShapeAgreement(BaseSchema):
    """Pooled shape agreement: mean IoU of matched pairs and shape-F1 (QA-2)."""

    mean_iou: float | None
    f1: float | None
    iou_threshold: float
    envelope_iou: bool = True


class SpanAgreement(BaseSchema):
    """Pooled span agreement: exact and overlap F1 (QA-2)."""

    f1_exact: float | None
    f1_overlap: float | None


class PairAgreement(BaseSchema):
    """Agreement between one pair of annotators.

    `items` is `None` on `ItemAgreement` (one item; the count would always be
    1) and the number of shared items on `ProjectAgreement`.
    """

    a: UUID
    b: UUID
    items: int | None = None
    cohen_kappa: float | None
    mean_iou: float | None
    shape_f1: float | None
    span_f1_exact: float | None
    span_f1_overlap: float | None


class ItemAgreement(BaseSchema):
    """Agreement over one item's consensus versions (embedded in `GET /items/{id}/consensus`)."""

    classification: list[ClassificationAgreement] = Field(default_factory=list)
    shapes: ShapeAgreement
    spans: SpanAgreement
    pairs: list[PairAgreement] = Field(default_factory=list)


class ProjectAgreement(ItemAgreement):
    """Project-wide inter-annotator agreement (`GET /projects/{id}/agreement`, QA-2)."""

    items: int
    annotators: list[AgreementAnnotator] = Field(default_factory=list)


class ConsensusAnnotatorRead(BaseSchema):
    """One consensus annotator's latest submitted version on an item."""

    user_id: UUID
    email: str
    display_name: str
    annotation_id: UUID
    version: int
    status: str
    created_at: datetime


class ConsensusRead(BaseSchema):
    """`GET /items/{id}/consensus` response body (QA-1, QA-2, QA-3)."""

    expected: int
    annotators: list[ConsensusAnnotatorRead]
    agreement: ItemAgreement
    preview: AnnotationResult
    conflicts: list[str]


class PickResolve(BaseSchema):
    """Resolve by copying one annotator's submitted consensus version verbatim."""

    method: Literal["pick"] = "pick"
    annotation_id: UUID


class FuseResolve(BaseSchema):
    """Resolve by fusing every submitted consensus version (QA-3)."""

    method: Literal["fuse"] = "fuse"
    iou_threshold: float = Field(default=0.5, gt=0.0, le=1.0)
    min_votes: int | None = Field(default=None, ge=1)
    comment: str | None = None


ResolveRequest = Annotated[PickResolve | FuseResolve, Field(discriminator="method")]


class ConsensusAnnotationRead(BaseSchema):
    """The annotation version `resolve` stores, mirroring `annotations.py`'s `AnnotationRead`."""

    id: UUID
    item_id: UUID
    task_id: UUID | None
    version: int
    source: str
    status: str
    author_user_id: UUID | None
    author_model_version_id: UUID | None
    label_schema_version_id: UUID
    duration_ms: int | None
    result: dict[str, Any]
    blob_path: str | None = None


class GoldSetRequest(BaseSchema):
    """`PUT /items/{id}/gold` payload: an approved primary version of this item."""

    annotation_id: UUID


class GoldTasksRequest(BaseSchema):
    """`POST /projects/{id}/gold/tasks` payload.

    Defaults (both omitted) to every member with role `annotator` times every
    item with a gold reference.
    """

    user_ids: list[UUID] | None = None
    item_ids: list[UUID] | None = None
    priority: int = 0


class GoldTasksResult(BaseSchema):
    """How many (item, user) gold tasks a `gold/tasks` request opened or skipped."""

    opened: int
    skipped: int


class AnnotatorQuality(BaseSchema):
    """One annotator's accuracy against gold references (QA-4)."""

    user_id: UUID
    email: str
    display_name: str
    gold_items: int
    classification_accuracy: float | None
    shape_f1: float | None
    mean_iou: float | None
    span_f1: float | None
    score: float | None


class AnnotatorQualityResponse(BaseSchema):
    """`GET /projects/{id}/quality/annotators` response body (QA-4)."""

    annotators: list[AnnotatorQuality]
