"""Project dashboard endpoint (UX-5). Read-only; the numbers come from `services/stats.py`."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.api.deps import CurrentUserDep, SessionDep
from app.schemas import ProjectStats
from app.services.repository import ensure_project_member, member_prefixes
from app.services.stats import DEFAULT_DAYS, MAX_DAYS, project_stats

router = APIRouter(prefix="/projects/{project_id}/stats", tags=["stats"])


@router.get("", response_model=ProjectStats, summary="Dashboard numbers for a project")
async def get_project_stats(
    project_id: UUID,
    session: SessionDep,
    current_user: CurrentUserDep,
    days: Annotated[int, Query(ge=1, le=MAX_DAYS)] = DEFAULT_DAYS,
) -> ProjectStats:
    await ensure_project_member(session, project_id, current_user)
    # A folder-limited member sees the numbers of their folders only.
    prefixes = await member_prefixes(session, project_id, current_user)
    return await project_stats(session, project_id, days=days, path_prefixes=prefixes)
