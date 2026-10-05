"""Consensus preview, project-wide agreement and resolution (QA-1, QA-2, QA-3).

Loads `consensus`-kind annotation versions from the database and hands them
to the pure functions in `services/agreement.py` and `services/fusion.py`.
`resolve_consensus` turns them into the item's `primary` annotation through
the same building blocks `services/reviewing.py`'s `apply_verdict` uses
(`create_version`, `review_item`, `record_review`, `notify_review`,
`emit_annotation_event`, `audit.record`) — but with its own self-review check,
since the fused version is always authored by the caller and the contract's
self-review rule is about whether the caller authored one of the *consensus*
versions being resolved, not about the fused version's (trivial) authorship.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ConflictError, ForbiddenError, NotFoundError
from app.models import (
    Annotation,
    AnnotationKind,
    AnnotationStatus,
    Item,
    ItemStatus,
    Task,
    TaskStatus,
    TaskType,
    User,
)
from app.schemas.annotation import AnnotationResult
from app.schemas.project import WorkflowConfig
from app.schemas.quality import (
    AgreementAnnotator,
    ConsensusAnnotatorRead,
    ConsensusRead,
    FuseResolve,
    PickResolve,
    ProjectAgreement,
)
from app.services import agreement, audit
from app.services.annotations import create_version, record_review
from app.services.comments import notify_review
from app.services.fusion import fuse
from app.services.item_flow import emit_annotation_event, review_item
from app.services.repository import path_scope_clause

__all__ = [
    "build_consensus_read",
    "load_item_consensus_versions",
    "load_project_consensus_versions",
    "project_agreement",
    "resolve_consensus",
]


async def load_item_consensus_versions(session: AsyncSession, item_id: UUID) -> list[Annotation]:
    """The latest *submitted* `consensus` version per author on one item (QA-1)."""
    rows = await session.scalars(
        select(Annotation)
        .where(
            Annotation.item_id == item_id,
            Annotation.kind == AnnotationKind.CONSENSUS,
            Annotation.status == AnnotationStatus.SUBMITTED,
        )
        .order_by(Annotation.author_user_id, Annotation.version.desc())
    )
    latest: dict[UUID, Annotation] = {}
    for row in rows:
        if row.author_user_id is not None and row.author_user_id not in latest:
            latest[row.author_user_id] = row
    return list(latest.values())


async def _load_users(session: AsyncSession, user_ids: Iterable[UUID]) -> dict[UUID, User]:
    ids = list(set(user_ids))
    if not ids:
        return {}
    rows = await session.scalars(select(User).where(User.id.in_(ids)))
    return {u.id: u for u in rows}


async def build_consensus_read(
    session: AsyncSession, *, item: Item, config: WorkflowConfig
) -> ConsensusRead:
    """`GET /items/{id}/consensus` (QA-1, QA-2, QA-3): agreement plus a fuse preview."""
    versions = await load_item_consensus_versions(session, item.id)
    if not versions:
        raise ConflictError(f"Item {item.id} has no submitted consensus version.")

    users = await _load_users(session, (v.author_user_id for v in versions if v.author_user_id))
    annotators = [
        ConsensusAnnotatorRead(
            user_id=v.author_user_id,
            email=users[v.author_user_id].email,
            display_name=users[v.author_user_id].display_name,
            annotation_id=v.id,
            version=v.version,
            status=v.status.value,
            created_at=v.created_at,
        )
        for v in versions
        if v.author_user_id is not None
    ]
    version_map = {
        v.author_user_id: AnnotationResult.model_validate(v.result)
        for v in versions
        if v.author_user_id is not None
    }
    item_agreement = agreement.compute_item_agreement(version_map)
    preview, conflicts = fuse(list(version_map.values()))

    return ConsensusRead(
        expected=config.consensus_annotators,
        annotators=annotators,
        agreement=item_agreement,
        preview=preview,
        conflicts=conflicts,
    )


async def _live_review_task(session: AsyncSession, item_id: UUID) -> Task | None:
    task: Task | None = await session.scalar(
        select(Task).where(
            Task.item_id == item_id,
            Task.type == TaskType.REVIEW,
            Task.status.in_((TaskStatus.OPEN, TaskStatus.IN_PROGRESS)),
        )
    )
    return task


async def resolve_consensus(
    session: AsyncSession,
    *,
    item: Item,
    role: str,
    config: WorkflowConfig,
    payload: PickResolve | FuseResolve,
    caller_id: UUID,
    organization_id: UUID,
    ip: str | None = None,
) -> Annotation:
    """`POST /items/{id}/consensus/resolve` (QA-3): fuse or pick, then approve.

    409 unless the item is `submitted` / `in_review` with >= 1 submitted
    consensus version; 403 when the project forbids self-review and the
    caller authored one of the consensus versions being resolved.
    """
    if item.status not in (ItemStatus.SUBMITTED, ItemStatus.IN_REVIEW):
        raise ConflictError(
            f"Item {item.id} is {item.status.value!r}; consensus can only be resolved while "
            "submitted or in review."
        )

    versions = await load_item_consensus_versions(session, item.id)
    if not versions:
        raise ConflictError(f"Item {item.id} has no submitted consensus version.")

    author_ids = {v.author_user_id for v in versions if v.author_user_id is not None}
    if not config.allow_self_review and caller_id in author_ids:
        raise ForbiddenError("This project does not allow reviewing your own annotation.")

    review_task = await _live_review_task(session, item.id)
    if review_task is None:
        raise ConflictError(f"Item {item.id} has no live review task.")

    comment: str | None = None
    if isinstance(payload, PickResolve):
        picked = next((v for v in versions if v.id == payload.annotation_id), None)
        if picked is None:
            raise NotFoundError(
                f"Consensus annotation {payload.annotation_id} does not exist on item {item.id}."
            )
        result = AnnotationResult.model_validate(picked.result)
        method = "pick"
    else:
        results = [AnnotationResult.model_validate(v.result) for v in versions]
        result, _conflicts = fuse(
            results, iou_threshold=payload.iou_threshold, min_votes=payload.min_votes
        )
        comment = payload.comment
        method = "fuse"

    submitted = await create_version(
        session,
        item=item,
        result=result,
        label_schema_version_id=versions[0].label_schema_version_id,
        author_user_id=caller_id,
        task_id=review_task.id,
        status=AnnotationStatus.SUBMITTED,
    )

    # Self-review is already checked against the consensus authors above, so
    # `review_item`'s own check (author_id == reviewer_id) must not fire here
    # — it would, always, since the fused version is authored by the caller.
    await review_item(
        session,
        item=item,
        role=role,
        config=config,
        approve=True,
        author_id=None,
        reviewer_id=caller_id,
    )
    outcome = await record_review(
        session,
        item=item,
        annotation=submitted,
        reviewer_id=caller_id,
        status=AnnotationStatus.APPROVED,
    )
    await notify_review(
        session, annotation=outcome, item=item, reviewer_id=caller_id, approve=True, comment=comment
    )
    await emit_annotation_event(
        session,
        item=item,
        annotation=outcome,
        event="approved",
        reviewer_id=str(caller_id),
        comment=comment,
        corrected=False,
        method=method,
    )
    audit.record(
        session,
        organization_id=organization_id,
        actor_id=caller_id,
        action="annotation.review",
        target_type="annotation",
        target_id=outcome.id,
        after={
            "approve": True,
            "status": outcome.status.value,
            "version": outcome.version,
            "method": method,
        },
        ip=ip,
    )
    return outcome


async def load_project_consensus_versions(
    session: AsyncSession,
    project_id: UUID,
    since: datetime | None = None,
    path_prefixes: list[str] | None = None,
) -> list[dict[UUID, AnnotationResult]]:
    """Per-item `{author: result}` maps for items with >= 2 submitted consensus versions.

    `since` limits to versions created after it (QA-2); the latest version
    per author is picked *after* that filter, matching "items whose versions
    were created after it".
    """
    stmt = (
        select(Annotation)
        .join(Item, Item.id == Annotation.item_id)
        .where(
            Item.project_id == project_id,
            Annotation.kind == AnnotationKind.CONSENSUS,
            Annotation.status == AnnotationStatus.SUBMITTED,
        )
    )
    if since is not None:
        stmt = stmt.where(Annotation.created_at > since)
    if path_prefixes:
        stmt = stmt.where(path_scope_clause(Item.path, path_prefixes))
    stmt = stmt.order_by(Annotation.item_id, Annotation.author_user_id, Annotation.version.desc())

    per_item: dict[UUID, dict[UUID, Annotation]] = {}
    for row in await session.scalars(stmt):
        if row.author_user_id is None:
            continue
        bucket = per_item.setdefault(row.item_id, {})
        if row.author_user_id not in bucket:
            bucket[row.author_user_id] = row

    result: list[dict[UUID, AnnotationResult]] = []
    for authors in per_item.values():
        if len(authors) < 2:
            continue
        result.append(
            {uid: AnnotationResult.model_validate(a.result) for uid, a in authors.items()}
        )
    return result


async def project_agreement(
    session: AsyncSession,
    project_id: UUID,
    *,
    since: datetime | None = None,
    iou_threshold: float = 0.5,
    path_prefixes: list[str] | None = None,
) -> ProjectAgreement:
    """`GET /projects/{id}/agreement` (QA-2): pooled inter-annotator agreement.

    `path_prefixes` (a folder-limited reviewer) pools only items under them.
    """
    items_versions = await load_project_consensus_versions(
        session, project_id, since=since, path_prefixes=path_prefixes
    )
    classification, shapes, spans, pairs, item_count = agreement.compute_project_agreement(
        items_versions, iou_threshold
    )
    counts = agreement.annotator_item_counts(items_versions)
    users = await _load_users(session, counts.keys())
    annotators = [
        AgreementAnnotator(
            user_id=uid,
            email=users[uid].email,
            display_name=users[uid].display_name,
            items=count,
        )
        for uid, count in sorted(counts.items(), key=lambda kv: str(kv[0]))
        if uid in users
    ]
    return ProjectAgreement(
        items=item_count,
        annotators=annotators,
        classification=classification,
        shapes=shapes,
        spans=spans,
        pairs=pairs,
    )
