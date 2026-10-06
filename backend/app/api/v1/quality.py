"""Quality control endpoints: consensus, agreement, gold (QA-1 … QA-4).

Thin by design, like `api/v1/annotations.py`: the invariants live in
`services/consensus.py` and `services/gold.py`. See CONTRACTS.md *Quality
control* and the REST rows for `/items/{id}/consensus`, `/items/{id}/gold`,
`/projects/{id}/gold/tasks`, `/projects/{id}/agreement` and
`/projects/{id}/quality/annotators`.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.deps import (
    UNRESTRICTED_LICENCE,
    ClientIpDep,
    CurrentUserDep,
    SessionDep,
    require_feature,
)
from app.api.errors import ForbiddenError, NotFoundError
from app.models import Item
from app.schemas import ItemRead
from app.schemas.quality import (
    AnnotatorQualityResponse,
    ConsensusAnnotationRead,
    ConsensusRead,
    GoldSetRequest,
    GoldTasksRequest,
    GoldTasksResult,
    ProjectAgreement,
    ResolveRequest,
)
from app.services import consensus, gold
from app.services.item_flow import load_workflow
from app.services.licensing.features import Feature
from app.services.repository import (
    ensure_item_member,
    ensure_project_member,
    member_prefixes,
)

#: Every route here is quality control, a Business feature (LIC-33).
router = APIRouter(tags=["quality"], dependencies=[require_feature(Feature.QUALITY)])

#: Roles allowed to see and act on quality-control data (QA-1 … QA-4).
_CAN_MANAGE_QUALITY = frozenset({"owner", "reviewer"})


async def _item_and_privileged_role(
    session: SessionDep, item_id: UUID, user: CurrentUserDep
) -> tuple[Item, str]:
    item = await session.get(Item, item_id)
    if item is None:
        raise NotFoundError(f"Item {item_id} does not exist.")
    role = await ensure_item_member(session, item, user)
    if role not in _CAN_MANAGE_QUALITY:
        raise ForbiddenError("Only a project owner or reviewer may access quality control.")
    return item, role


@router.get(
    "/items/{item_id}/consensus",
    response_model=ConsensusRead,
    summary="Consensus versions, agreement and a fuse preview for one item",
)
async def get_consensus(
    item_id: UUID, session: SessionDep, current_user: CurrentUserDep
) -> ConsensusRead:
    """Owner / reviewer (QA-1, QA-2, QA-3): 409 when the item has no submitted consensus version."""
    item, _role = await _item_and_privileged_role(session, item_id, current_user)
    config = await load_workflow(session, item.project_id)
    return await consensus.build_consensus_read(session, item=item, config=config)


@router.post(
    "/items/{item_id}/consensus/resolve",
    dependencies=[UNRESTRICTED_LICENCE],
    response_model=ConsensusAnnotationRead,
    summary="Resolve an item's consensus versions into its primary annotation",
)
async def resolve_consensus(
    item_id: UUID,
    payload: ResolveRequest,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> ConsensusAnnotationRead:
    """Owner / reviewer (QA-3): pick one version or fuse them all, then approve.

    409 unless the item is `submitted` / `in_review` with >= 1 submitted
    consensus version; 403 when the project forbids self-review and the
    caller authored one of the consensus versions.
    """
    item, role = await _item_and_privileged_role(session, item_id, current_user)
    config = await load_workflow(session, item.project_id)

    outcome = await consensus.resolve_consensus(
        session,
        item=item,
        role=role,
        config=config,
        payload=payload,
        caller_id=current_user.id,
        organization_id=current_user.organization_id,
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(outcome)
    return ConsensusAnnotationRead.model_validate(outcome, from_attributes=True)


@router.put(
    "/items/{item_id}/gold",
    response_model=ItemRead,
    summary="Set an item's gold reference",
)
async def set_gold(
    item_id: UUID,
    payload: GoldSetRequest,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> ItemRead:
    """Owner / reviewer (QA-4): 422 unless `annotation_id` is an approved primary version."""
    item, _role = await _item_and_privileged_role(session, item_id, current_user)
    await gold.set_gold_reference(
        session,
        item=item,
        annotation_id=payload.annotation_id,
        actor_id=current_user.id,
        organization_id=current_user.organization_id,
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(item)
    return ItemRead.model_validate(item)


@router.delete(
    "/items/{item_id}/gold",
    response_model=ItemRead,
    summary="Clear an item's gold reference",
)
async def clear_gold(
    item_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
    client_ip: ClientIpDep,
) -> ItemRead:
    """Owner / reviewer (QA-4): also cancels the item's live gold tasks."""
    item, _role = await _item_and_privileged_role(session, item_id, current_user)
    await gold.clear_gold_reference(
        session,
        item=item,
        actor_id=current_user.id,
        organization_id=current_user.organization_id,
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(item)
    return ItemRead.model_validate(item)


@router.post(
    "/projects/{project_id}/gold/tasks",
    dependencies=[UNRESTRICTED_LICENCE],
    response_model=GoldTasksResult,
    summary="Open gold tasks for a project",
)
async def open_gold_tasks(
    project_id: UUID,
    payload: GoldTasksRequest,
    session: SessionDep,
    current_user: CurrentUserDep,
) -> GoldTasksResult:
    """Owner / reviewer (QA-4). Defaults to every `annotator` member x every gold item."""
    role = await ensure_project_member(session, project_id, current_user)
    if role not in _CAN_MANAGE_QUALITY:
        raise ForbiddenError("Only a project owner or reviewer may open gold tasks.")

    opened, skipped = await gold.open_gold_tasks(
        session,
        project_id=project_id,
        user_ids=payload.user_ids,
        item_ids=payload.item_ids,
        priority=payload.priority,
    )
    await session.commit()
    return GoldTasksResult(opened=opened, skipped=skipped)


@router.get(
    "/projects/{project_id}/agreement",
    response_model=ProjectAgreement,
    summary="Project-wide inter-annotator agreement",
)
async def get_project_agreement(
    project_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
    since: datetime | None = Query(default=None),
) -> ProjectAgreement:
    """Owner / reviewer (QA-2): pooled over items with >= 2 submitted consensus versions."""
    role = await ensure_project_member(session, project_id, current_user)
    if role not in _CAN_MANAGE_QUALITY:
        raise ForbiddenError("Only a project owner or reviewer may see project agreement.")
    prefixes = await member_prefixes(session, project_id, current_user)
    return await consensus.project_agreement(
        session, project_id, since=since, path_prefixes=prefixes
    )


@router.get(
    "/projects/{project_id}/quality/annotators",
    response_model=AnnotatorQualityResponse,
    summary="Per-annotator accuracy against gold references",
)
async def get_annotator_quality(
    project_id: UUID, session: SessionDep, current_user: CurrentUserDep
) -> AnnotatorQualityResponse:
    """Owner / reviewer (QA-4)."""
    role = await ensure_project_member(session, project_id, current_user)
    if role not in _CAN_MANAGE_QUALITY:
        raise ForbiddenError("Only a project owner or reviewer may see annotator quality.")
    prefixes = await member_prefixes(session, project_id, current_user)
    annotators = await gold.project_annotator_quality(session, project_id, path_prefixes=prefixes)
    return AnnotatorQualityResponse(annotators=annotators)


__all__ = ["router"]
