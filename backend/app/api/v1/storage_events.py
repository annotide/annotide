"""Storage-event receiver and its token (SRC-3).

`POST /connectors/{id}/events` is called by an object store's notification
service, not by a person: it carries no session, only the connector's event
token, and every way of getting that wrong — an unknown connector, events
turned off, a stale token — is the same 404, so the route tells a prober
nothing. Minting and revoking the token is system administration, like the
rest of `/connectors`.
"""

from __future__ import annotations

import json
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import APIRouter, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    ClientIpDep,
    LicenceDep,
    QueueDep,
    SessionDep,
    SettingsDep,
    SuperuserDep,
    enforce_rate_limit,
)
from app.api.errors import (
    NotFoundError,
    PayloadTooLargeError,
    ServiceUnavailableError,
    ValidationFailedError,
)
from app.core.logging import get_logger
from app.models import Connector, JobType
from app.schemas import BaseSchema
from app.services import audit, discovery
from app.services.jobs import submit_job
from app.services.licensing.state import is_restricted
from app.services.repository import get_or_404

router = APIRouter(prefix="/connectors", tags=["connectors"])

log = get_logger(__name__)

TokenQuery = Annotated[str | None, Query(alias="token")]
TokenHeader = Annotated[str | None, Header(alias="X-Event-Token")]


class EventTokenRead(BaseSchema):
    """A freshly minted storage-event token (SRC-3), shown once."""

    token: str
    #: Where the store should POST, token in `?token=` or `X-Event-Token`.
    path: str


class EventDeliveryResult(BaseSchema):
    """What one storage-event delivery queued (SRC-3)."""

    received: int
    matched: int
    ignored: int
    job_ids: list[UUID]


def _events_path(connector_id: UUID) -> str:
    return f"/api/v1/connectors/{connector_id}/events"


@router.post(
    "/{connector_id}/events/token",
    response_model=EventTokenRead,
    status_code=status.HTTP_201_CREATED,
    summary="Mint the connector's storage-event token (system administrators only, SRC-3)",
)
async def mint_event_token(
    connector_id: UUID,
    session: SessionDep,
    current_user: SuperuserDep,
    client_ip: ClientIpDep,
) -> EventTokenRead:
    """Turn storage events on, or replace the token; the old one stops working at once."""
    connector = await get_or_404(
        session, Connector, connector_id, organization_id=current_user.organization_id
    )
    token, digest = discovery.new_event_token()
    replaced = connector.event_token_hash is not None
    connector.event_token_hash = digest
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="connector.events_token",
        target_type="connector",
        target_id=connector.id,
        after={"events_enabled": True, "replaced": replaced},
        ip=client_ip,
    )
    await session.commit()
    return EventTokenRead(token=token, path=_events_path(connector.id))


@router.delete(
    "/{connector_id}/events/token",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Turn the connector's storage events off (system administrators only, SRC-3)",
)
async def revoke_event_token(
    connector_id: UUID,
    session: SessionDep,
    current_user: SuperuserDep,
    client_ip: ClientIpDep,
) -> None:
    """Forget the token; deliveries answer 404 from now on."""
    connector = await get_or_404(
        session, Connector, connector_id, organization_id=current_user.organization_id
    )
    if connector.event_token_hash is None:
        return
    connector.event_token_hash = None
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="connector.events_token",
        target_type="connector",
        target_id=connector.id,
        after={"events_enabled": False},
        ip=client_ip,
    )
    await session.commit()


async def _read_capped(request: Request, limit: int) -> bytes:
    """The body, refused with 413 as soon as it passes `limit` rather than after buffering it."""
    too_large = PayloadTooLargeError(f"Event deliveries are limited to {limit} bytes.")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_large
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise too_large
    return bytes(body)


async def _receiving_connector(
    session: AsyncSession, connector_id: UUID, token: str | None
) -> Connector:
    connector = await session.get(Connector, connector_id)
    if connector is None or not discovery.token_matches(connector, token):
        raise NotFoundError("No storage-event endpoint here.")
    return connector


@router.options(
    "/{connector_id}/events",
    response_model=None,
    summary="CloudEvents webhook validation handshake (SRC-3)",
)
async def event_handshake(
    connector_id: UUID,
    session: SessionDep,
    token: TokenQuery = None,
    header_token: TokenHeader = None,
    origin: Annotated[str | None, Header(alias="WebHook-Request-Origin")] = None,
) -> Response:
    """Allow the origin Event Grid asks about (CloudEvents abuse protection)."""
    await _receiving_connector(session, connector_id, token or header_token)
    response = Response(status_code=status.HTTP_200_OK)
    if origin:
        response.headers["WebHook-Allowed-Origin"] = origin
        response.headers["WebHook-Allowed-Rate"] = "*"
    return response


@router.post(
    "/{connector_id}/events",
    response_model=EventDeliveryResult,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Receive storage events and scan the objects they name (SRC-3)",
)
async def receive_events(
    connector_id: UUID,
    request: Request,
    session: SessionDep,
    queue: QueueDep,
    settings: SettingsDep,
    licence: LicenceDep,
    token: TokenQuery = None,
    header_token: TokenHeader = None,
) -> EventDeliveryResult | JSONResponse:
    """Queue one `scan_source` job per project that should pick up a created object.

    Read as raw bytes, not a JSON body: SNS posts `text/plain`. Handshakes
    (Event Grid validation, SNS confirmation) answer 200 and queue nothing.
    """
    connector = await _receiving_connector(session, connector_id, token or header_token)
    await enforce_rate_limit(
        settings, "storage_events", str(connector.id), settings.rate_limit_per_minute
    )

    body = await _read_capped(request, discovery.MAX_EVENT_BODY_BYTES)
    try:
        delivery = discovery.parse_delivery(json.loads(body))
    except (ValueError, RecursionError) as exc:  # EventFormatError, not JSON, nested too deep
        raise ValidationFailedError(f"Unrecognised storage event: {exc}") from exc

    if delivery.validation_code is not None:
        log.info("storage_events.validated", connector_id=str(connector.id))
        return JSONResponse({"validationResponse": delivery.validation_code})
    if delivery.subscribe_url is not None:
        try:
            await discovery.confirm_sns_subscription(delivery.subscribe_url)
        except discovery.EventFormatError as exc:
            raise ValidationFailedError(str(exc)) from exc
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError(
                f"Could not confirm the SNS subscription ({type(exc).__name__})."
            ) from exc
        log.info("storage_events.sns_confirmed", connector_id=str(connector.id))
        return JSONResponse({"confirmed": True})

    if len(delivery.events) > discovery.MAX_EVENT_OBJECTS:
        raise PayloadTooLargeError(
            f"Event deliveries are limited to {discovery.MAX_EVENT_OBJECTS} objects."
        )
    # A scan opens tasks, which restricted mode refuses (LIC-5); a 503 makes
    # the sender retry, so nothing is lost while a renewed key is installed.
    if is_restricted(licence) and licence.license is not None:
        raise ServiceUnavailableError(
            "The licence has expired; storage events are not processed until it is renewed."
        )

    routed = await discovery.route_events(session, connector, delivery.events)
    job_ids: list[UUID] = []
    for project_id, paths in routed.items():
        job = await submit_job(
            session,
            queue,
            project_id=project_id,
            job_type=JobType.SCAN_SOURCE,
            payload={"paths": paths, "trigger": "event"},
        )
        job_ids.append(job.id)

    matched = len({path for paths in routed.values() for path in paths})
    log.info(
        "storage_events.received",
        connector_id=str(connector.id),
        received=len(delivery.events),
        matched=matched,
        jobs=len(job_ids),
    )
    return EventDeliveryResult(
        received=len(delivery.events),
        matched=matched,
        ignored=len(delivery.events) - matched,
        job_ids=job_ids,
    )
