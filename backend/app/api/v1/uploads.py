"""Browser uploads to a project's source connector (§12 upload path).

Mints write-scoped signed URLs; the bytes never pass through here. See
`services/uploads.py` for the flow and `api/v1/storage.py` for the local
disk proxy that the `local` connector's URLs point at.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from app.api.deps import CurrentUserDep, SessionDep, SettingsDep
from app.api.errors import ConflictError, ForbiddenError, ValidationFailedError
from app.connectors.errors import ConnectorError, UnsupportedOperation
from app.models import Connector, Project
from app.schemas import UploadUrlsRequest, UploadUrlsResponse
from app.services.repository import ensure_project_member, get_or_404
from app.services.secrets import SecretResolutionError
from app.services.uploads import InvalidUploadPath, join_prefix, mint_upload_targets

router = APIRouter(tags=["uploads"])


@router.post(
    "/projects/{project_id}/uploads",
    response_model=UploadUrlsResponse,
    status_code=status.HTTP_200_OK,
    summary="Mint write-scoped signed URLs for uploading files to the source connector",
)
async def create_upload_urls(
    project_id: UUID,
    payload: UploadUrlsRequest,
    session: SessionDep,
    settings: SettingsDep,
    current_user: CurrentUserDep,
) -> UploadUrlsResponse:
    """One signed URL per file, valid for `APP_SIGNED_URL_TTL` seconds.

    Owner only: uploads add objects to the customer's own store. The
    browser PUTs each file to its `url` with `headers`, then queues a scan
    (`POST /projects/{id}/scan`) so the new objects become items.
    """
    role = await ensure_project_member(session, project_id, current_user)
    if role != "owner":
        raise ForbiddenError("Only a project owner may upload to the source connector.")

    project = await get_or_404(
        session, Project, project_id, organization_id=current_user.organization_id
    )
    if project.source_connector_id is None:
        raise ConflictError("The project has no source connector to upload to.")
    connector = await get_or_404(session, Connector, project.source_connector_id)

    try:
        targets = await mint_upload_targets(
            project, connector, payload.files, expires_in=settings.signed_url_ttl
        )
    except InvalidUploadPath as exc:
        raise ValidationFailedError(str(exc)) from exc
    except UnsupportedOperation as exc:
        raise ConflictError(
            f"The source connector ({connector.type}) cannot issue upload URLs: {exc}"
        ) from exc
    except (ConnectorError, SecretResolutionError) as exc:
        raise ConflictError(f"Could not sign upload URLs on the source connector: {exc}") from exc

    return UploadUrlsResponse(prefix=join_prefix(project.source_prefix, ""), uploads=targets)
