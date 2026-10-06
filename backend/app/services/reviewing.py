"""A reviewer's verdict on one submitted annotation (WF-4), shared by the
single-item review endpoint and the bulk approve action (WF-8).

`apply_verdict` sequences everything a verdict entails — the item / task
transition, the optional comment and correction, the blob re-publish, the
notification and the audit row — so the two entry points cannot drift apart.
The caller commits.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ConflictError
from app.models import Annotation, AnnotationKind, AnnotationStatus, Comment, Item
from app.schemas import AnnotationResult
from app.services import audit
from app.services.annotations import create_version, record_review
from app.services.comments import notify_review
from app.services.item_flow import emit_annotation_event, review_item
from app.services.workflow import WorkflowConfig


async def apply_verdict(
    session: AsyncSession,
    *,
    annotation: Annotation,
    item: Item,
    role: str,
    config: WorkflowConfig,
    reviewer_id: UUID,
    organization_id: UUID,
    approve: bool,
    comment: str | None = None,
    corrected_result: AnnotationResult | None = None,
    ip: str | None = None,
) -> Annotation:
    """Approve or reject `annotation`, which must be `submitted` (409 otherwise).

    Approving with `corrected_result` stores the correction as a new version
    authored by the reviewer, so the annotator's original stays intact and
    the difference between them is recoverable. Returns the version that now
    carries the verdict: the original, or the correction.
    """
    if annotation.status is not AnnotationStatus.SUBMITTED:
        raise ConflictError(
            f"Only a submitted annotation can be reviewed; this one is {annotation.status.value!r}."
        )
    if annotation.kind is not AnnotationKind.PRIMARY:
        raise ConflictError(
            f"Only a primary annotation can be reviewed; this one is {annotation.kind.value!r}."
        )

    # Raises WorkflowError -> 409 when the caller's role may not do this, and
    # 403 for a self-review the project forbids (WF-1). Closes the review task
    # and, on rejection, reopens annotation for whoever the config says.
    await review_item(
        session,
        item=item,
        role=role,
        config=config,
        approve=approve,
        author_id=annotation.author_user_id,
        reviewer_id=reviewer_id,
    )

    if comment:
        # The verdict's reasoning lives in the comment thread on the reviewed
        # version (WF-5), where the annotator who gets the item back can read it.
        session.add(
            Comment(
                project_id=item.project_id,
                item_id=item.id,
                annotation_id=annotation.id,
                author_id=reviewer_id,
                body=comment,
            )
        )

    verdict = AnnotationStatus.APPROVED if approve else AnnotationStatus.REJECTED
    if corrected_result is not None:
        outcome = await create_version(
            session,
            item=item,
            result=corrected_result,
            label_schema_version_id=annotation.label_schema_version_id,
            author_user_id=reviewer_id,
            task_id=annotation.task_id,
            status=verdict,
        )
    else:
        # In-place status change; emits an outbox event so the blob copy of
        # this version carries the verdict too.
        outcome = await record_review(
            session,
            item=item,
            annotation=annotation,
            reviewer_id=reviewer_id,
            status=verdict,
        )

    # The annotator learns the verdict in-app (WF-5); a rejected item is
    # already back in their queue, this tells them why.
    await notify_review(
        session,
        annotation=annotation,
        item=item,
        reviewer_id=reviewer_id,
        approve=approve,
        comment=comment,
    )
    await emit_annotation_event(
        session,
        item=item,
        annotation=outcome,
        event="approved" if approve else "rejected",
        reviewer_id=str(reviewer_id),
        comment=comment,
        corrected=corrected_result is not None,
    )
    audit.record(
        session,
        organization_id=organization_id,
        actor_id=reviewer_id,
        action="annotation.review",
        target_type="annotation",
        target_id=outcome.id,
        after={
            "approve": approve,
            "status": outcome.status.value,
            "version": outcome.version,
            "corrected": corrected_result is not None,
        },
        ip=ip,
    )
    return outcome
