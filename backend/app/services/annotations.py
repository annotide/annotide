"""Annotation versioning, validation and the outbox write (DATA-1, DATA-2, QA-6).

Three invariants live here, and all three are easy to break from a router:

1. **Annotations are never updated in place.** A change is a new row with
   ``version + 1`` (DATA-1). The history is the audit trail, and a reviewer's
   diff is computed from it.

2. **The database row and the blob write happen together.** They cannot be one
   transaction — one is Postgres, the other is object storage — so the write
   goes through a transactional outbox (DATA-2): the annotation row and an
   ``outbox_event`` row are inserted in the same transaction, and the worker
   publishes to blob afterwards. Either both exist or neither does.

3. **Submitting runs the schema validation** (QA-6). Drafts may be incomplete;
   a submitted annotation may not be.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ConflictError, NotFoundError, ValidationFailedError
from app.models import (
    Annotation,
    AnnotationKind,
    AnnotationSource,
    AnnotationStatus,
    Item,
    LabelSchemaVersion,
    OutboxEvent,
    Task,
    TaskStatus,
    TaskType,
)
from app.schemas import AnnotationResult, LabelSchemaDefinition, validate_against_schema

#: Outbox event emitted once an annotation version is durable in Postgres.
ANNOTIDE_WRITTEN = "annotation.written"


def blob_path_for(project_id: UUID, item_id: UUID, version: int) -> str:
    """Where this version lands in the result container (see CONTRACTS Blob layout)."""
    return f"annotations/{project_id}/{item_id}/v{version}.json"


async def next_version(session: AsyncSession, item_id: UUID) -> int:
    """The next version number for an item, computed in SQL.

    Read-modify-write in Python would race two concurrent submits into the same
    version; the unique constraint on ``(item_id, version)`` would then reject
    one of them with an integrity error instead of a useful message.
    """
    current = await session.scalar(
        select(func.max(Annotation.version)).where(Annotation.item_id == item_id)
    )
    return int(current or 0) + 1


async def validate_result(
    session: AsyncSession,
    result: AnnotationResult,
    label_schema_version_id: UUID,
) -> list[str]:
    """Check an annotation against its label schema version (QA-6).

    Returns a list of human-readable violations; empty means valid.
    """
    schema_version = await session.get(LabelSchemaVersion, label_schema_version_id)
    if schema_version is None:
        raise NotFoundError(f"Label schema version {label_schema_version_id} does not exist.")

    definition = LabelSchemaDefinition.model_validate(schema_version.definition)
    return validate_against_schema(result, definition)


def build_blob_document(
    *,
    item: Item,
    annotation: Annotation,
    result: AnnotationResult,
    reviewer_id: UUID | None = None,
) -> dict[str, Any]:
    """The self-contained JSON written to blob storage (DATA-3).

    Self-contained means a reader needs nothing but this document: the item's
    path, the schema version, who annotated and who reviewed, and the result.
    The database can be rebuilt from these files alone (DATA-6).
    """
    return {
        "item_id": str(item.id),
        "item_path": item.path,
        "project_id": str(item.project_id),
        "connector_id": str(item.connector_id),
        "version": annotation.version,
        "label_schema_version_id": str(annotation.label_schema_version_id),
        "source": annotation.source.value,
        "annotator_id": str(annotation.author_user_id) if annotation.author_user_id else None,
        "model_version_id": (
            str(annotation.author_model_version_id) if annotation.author_model_version_id else None
        ),
        "reviewer_id": str(reviewer_id) if reviewer_id else None,
        "status": annotation.status.value,
        "duration_ms": annotation.duration_ms,
        "result": result.model_dump(mode="json", by_alias=True),
    }


async def create_version(
    session: AsyncSession,
    *,
    item: Item,
    result: AnnotationResult,
    label_schema_version_id: UUID,
    author_user_id: UUID | None = None,
    author_model_version_id: UUID | None = None,
    task_id: UUID | None = None,
    duration_ms: int | None = None,
    status: AnnotationStatus = AnnotationStatus.DRAFT,
    kind: AnnotationKind = AnnotationKind.PRIMARY,
) -> Annotation:
    """Write a new annotation version and its outbox event in one transaction.

    The caller commits. Both rows are flushed here so the annotation's id is
    available for the outbox payload, but they share the caller's transaction —
    that is the whole point of the outbox pattern.

    `kind` (QA-1, QA-4) marks a `consensus` or `gold` attempt; only `primary`
    versions are ever published to the result connector — a `consensus` /
    `gold` version gets no outbox event at all, since "never published" is
    absolute (CONTRACTS.md *annotation*).
    """
    if (author_user_id is None) == (author_model_version_id is None):
        # Mirrors the CHECK constraint on the table; caught here so the caller
        # gets a clear message instead of an integrity error at commit.
        raise ConflictError(
            "An annotation has exactly one author: either a user or a model version."
        )

    if status is not AnnotationStatus.DRAFT:
        violations = await validate_result(session, result, label_schema_version_id)
        if violations:
            raise ValidationFailedError(
                f"The annotation has {len(violations)} validation problem(s).",
                extra={"violations": violations},
            )

    version = await next_version(session, item.id)
    source = AnnotationSource.HUMAN if author_user_id else AnnotationSource.MODEL

    annotation = Annotation(
        item_id=item.id,
        task_id=task_id,
        version=version,
        author_user_id=author_user_id,
        author_model_version_id=author_model_version_id,
        source=source,
        label_schema_version_id=label_schema_version_id,
        result=result.model_dump(mode="json", by_alias=True),
        status=status,
        kind=kind,
        duration_ms=duration_ms,
    )
    session.add(annotation)
    # Flush, not commit: the outbox row must join the same transaction.
    await session.flush()

    if kind is AnnotationKind.PRIMARY:
        session.add(
            OutboxEvent(
                aggregate_type="annotation",
                aggregate_id=annotation.id,
                type=ANNOTIDE_WRITTEN,
                payload={
                    "blob_path": blob_path_for(item.project_id, item.id, version),
                    "document": build_blob_document(
                        item=item, annotation=annotation, result=result
                    ),
                },
            )
        )

    return annotation


def task_kind(task: Task | None) -> AnnotationKind:
    """Which `AnnotationKind` a version saved under `task` gets (CONTRACTS.md *task*)."""
    if task is None:
        return AnnotationKind.PRIMARY
    if task.gold:
        return AnnotationKind.GOLD
    if task.slot is not None:
        return AnnotationKind.CONSENSUS
    return AnnotationKind.PRIMARY


async def resolve_annotate_task(
    session: AsyncSession,
    *,
    item_id: UUID,
    user_id: UUID,
    task_id: UUID | None,
) -> Task | None:
    """Which annotate task a save belongs to (CONTRACTS.md *task*, last paragraph).

    The `task_id` in the create body when given — it must be the caller's own
    live (`open` or `in_progress`) annotate task on this item, else
    `ConflictError` (409) — otherwise the caller's own `in_progress` annotate
    task on the item, otherwise `None` (an ordinary `primary` save, as
    before).
    """
    if task_id is not None:
        task = await session.get(Task, task_id)
        if (
            task is None
            or task.item_id != item_id
            or task.type is not TaskType.ANNOTATE
            or task.assignee_id != user_id
            or task.status not in (TaskStatus.OPEN, TaskStatus.IN_PROGRESS)
        ):
            raise ConflictError(f"Task {task_id} is not your live annotate task on this item.")
        return task

    own_task: Task | None = await session.scalar(
        select(Task).where(
            Task.item_id == item_id,
            Task.type == TaskType.ANNOTATE,
            Task.status == TaskStatus.IN_PROGRESS,
            Task.assignee_id == user_id,
        )
    )
    return own_task


async def record_review(
    session: AsyncSession,
    *,
    item: Item,
    annotation: Annotation,
    reviewer_id: UUID,
    status: AnnotationStatus,
) -> Annotation:
    """Set a reviewed version's status in place and re-publish its blob.

    Review does not create a new version (unless the reviewer corrects the
    result, which goes through `create_version`), so the row's `status` moves
    from `submitted` to `approved`/`rejected` in place. The blob copy must
    follow: without a fresh outbox event it would keep saying `submitted`,
    and a database rebuilt from blobs (DATA-6) would lose every verdict. The
    event targets the same `blob_path` as the original write, so the
    publisher overwrites the document rather than adding one.

    The caller commits; the event joins its transaction like `create_version`.
    """
    annotation.status = status
    result = AnnotationResult.model_validate(annotation.result)
    session.add(
        OutboxEvent(
            aggregate_type="annotation",
            aggregate_id=annotation.id,
            type=ANNOTIDE_WRITTEN,
            payload={
                "blob_path": blob_path_for(item.project_id, item.id, annotation.version),
                "document": build_blob_document(
                    item=item, annotation=annotation, result=result, reviewer_id=reviewer_id
                ),
            },
        )
    )
    return annotation


async def latest_version(session: AsyncSession, item_id: UUID) -> Annotation | None:
    """The most recent *primary* annotation version for an item, if any.

    Consensus and gold attempts (QA-1, QA-4) are never the item's annotation.
    """
    result: Annotation | None = await session.scalar(
        select(Annotation)
        .where(Annotation.item_id == item_id, Annotation.kind == AnnotationKind.PRIMARY)
        .order_by(Annotation.version.desc())
        .limit(1)
    )
    return result


def diff_shapes(before: AnnotationResult, after: AnnotationResult) -> dict[str, list[str]]:
    """Compare two versions by shape id (WF-4).

    Shape ids are client-generated and stable across versions precisely so a
    reviewer's edits can be shown as added / removed / changed rather than as a
    wholesale replacement.
    """
    before_by_id = {shape.id: shape for shape in before.shapes}
    after_by_id = {shape.id: shape for shape in after.shapes}

    # Ids are stringified for the JSON response; they are UUIDs on the models.
    added = sorted(str(i) for i in set(after_by_id) - set(before_by_id))
    removed = sorted(str(i) for i in set(before_by_id) - set(after_by_id))
    changed = sorted(
        str(shape_id)
        for shape_id in set(before_by_id) & set(after_by_id)
        if before_by_id[shape_id] != after_by_id[shape_id]
    )

    return {"added": added, "removed": removed, "changed": changed}
