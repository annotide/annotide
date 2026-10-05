"""Storage connector management endpoints (§11 REST API).

Connectors describe where a project's source media and result annotations
live. Registering and testing one is system administration, not project
work, so every endpoint here requires `SuperuserDep`. The storage-event
receiver, which a store calls without a session, is in `storage_events`.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, status
from sqlalchemy import select

from app.api.deps import ClientIpDep, LicenceDep, PageParamsDep, SessionDep, SuperuserDep
from app.api.errors import LicenceFeatureError
from app.connectors.registry import build_connector
from app.models import Connector
from app.models import ConnectorIdentity as DbConnectorIdentity
from app.models import ConnectorType as DbConnectorType
from app.schemas import BaseSchema, ConnectorCreate, ConnectorRead, ConnectorUpdate, Page
from app.services import audit
from app.services.licensing.features import Feature, has_feature
from app.services.licensing.features import refusal_message as feature_refusal
from app.services.licensing.state import EffectiveLicense
from app.services.repository import get_or_404, paginate
from app.services.secrets import SecretResolutionError, resolve_secret
from app.services.storage import connector_pool

router = APIRouter(prefix="/connectors", tags=["connectors"])

_CHECK_TIMEOUT_SECONDS = 10


class ConnectorCheckResult(BaseSchema):
    """Result of testing a connector's connectivity (SRC-7)."""

    ok: bool
    messages: list[str]


def _to_read(connector: Connector) -> ConnectorRead:
    """Build the public representation of a connector.

    Never includes `secret_ref` (a Key Vault reference): only whether one is
    set, via `has_secret`.
    """
    return ConnectorRead(
        id=connector.id,
        organization_id=connector.organization_id,
        name=connector.name,
        type=connector.type,
        identity_type=connector.identity_type,
        has_secret=connector.secret_ref is not None,
        events_enabled=connector.event_token_hash is not None,
        config=connector.config,
        created_at=connector.created_at,
        updated_at=connector.updated_at,
    )


@router.get(
    "",
    response_model=Page[ConnectorRead],
    summary="List connectors in the caller's organization (system administrators only)",
)
async def list_connectors(
    session: SessionDep,
    current_user: SuperuserDep,
    page_params: PageParamsDep,
) -> Page[ConnectorRead]:
    """List connectors, newest first, scoped to the caller's organization."""
    stmt = select(Connector).where(Connector.organization_id == current_user.organization_id)
    result = await paginate(
        session,
        stmt,
        limit=page_params.limit,
        cursor=page_params.cursor,
        order_by=(Connector.created_at, Connector.id),
        key_of=lambda row: (row.created_at, row.id),
        direction="desc",
    )
    return Page[ConnectorRead](
        items=[_to_read(row) for row in result.items],
        next_cursor=result.next_cursor,
    )


def _audit_after(connector: Connector) -> dict[str, object]:
    """Audit `after` snapshot: never `secret_ref`, only whether one is set."""
    return {
        "name": connector.name,
        "type": connector.type.value,
        "identity_type": connector.identity_type.value,
        "has_secret": connector.secret_ref is not None,
    }


@router.post(
    "",
    response_model=ConnectorRead,
    status_code=status.HTTP_201_CREATED,
    summary="Register a connector (system administrators only)",
)
async def create_connector(
    payload: ConnectorCreate,
    session: SessionDep,
    current_user: SuperuserDep,
    client_ip: ClientIpDep,
    licence: LicenceDep,
) -> ConnectorRead:
    """Register a new storage connector.

    `secret_ref` is a Key Vault reference; the database never stores the
    secret itself (SEC/AUTH-7).
    """
    _require_type_licensed(DbConnectorType(payload.type.value), licence)
    connector = Connector(
        organization_id=current_user.organization_id,
        name=payload.name,
        type=DbConnectorType(payload.type.value),
        identity_type=DbConnectorIdentity(payload.identity_type.value),
        secret_ref=payload.secret_ref,
        config=payload.config,
    )
    session.add(connector)
    await session.flush()
    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="connector.create",
        target_type="connector",
        target_id=connector.id,
        after=_audit_after(connector),
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(connector)
    return _to_read(connector)


@router.get(
    "/{connector_id}",
    response_model=ConnectorRead,
    summary="Get a connector by id (system administrators only)",
)
async def get_connector(
    connector_id: UUID,
    session: SessionDep,
    current_user: SuperuserDep,
) -> ConnectorRead:
    """Return one connector."""
    connector = await get_or_404(
        session, Connector, connector_id, organization_id=current_user.organization_id
    )
    return _to_read(connector)


@router.patch(
    "/{connector_id}",
    response_model=ConnectorRead,
    summary="Update a connector (system administrators only)",
)
async def update_connector(
    connector_id: UUID,
    payload: ConnectorUpdate,
    session: SessionDep,
    current_user: SuperuserDep,
    client_ip: ClientIpDep,
    licence: LicenceDep,
) -> ConnectorRead:
    """Partially update a connector; an omitted field is left untouched."""
    connector = await get_or_404(
        session, Connector, connector_id, organization_id=current_user.organization_id
    )

    updates = payload.model_dump(exclude_unset=True)
    if updates.get("type") is not None:
        updates["type"] = DbConnectorType(updates["type"])
        if updates["type"] != connector.type:
            _require_type_licensed(updates["type"], licence)
    if updates.get("identity_type") is not None:
        updates["identity_type"] = DbConnectorIdentity(updates["identity_type"])
    for field, value in updates.items():
        setattr(connector, field, value)

    audit.record(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        action="connector.update",
        target_type="connector",
        target_id=connector.id,
        after=_audit_after(connector),
        ip=client_ip,
    )
    await session.commit()
    await session.refresh(connector)
    return _to_read(connector)


@router.delete(
    "/{connector_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a connector (system administrators only)",
)
async def delete_connector(
    connector_id: UUID,
    session: SessionDep,
    current_user: SuperuserDep,
) -> None:
    """Delete a connector."""
    connector = await get_or_404(
        session, Connector, connector_id, organization_id=current_user.organization_id
    )
    await session.delete(connector)
    await session.commit()
    connector_pool().discard(connector_id)


@router.post(
    "/{connector_id}/check",
    response_model=ConnectorCheckResult,
    summary="Test a connector's connectivity (system administrators only, SRC-7)",
)
async def check_connector(
    connector_id: UUID,
    session: SessionDep,
    current_user: SuperuserDep,
    client_ip: ClientIpDep,
) -> ConnectorCheckResult:
    """Run the connector's `check()` against its backing store.

    A misconfigured connector must never fail the request: an unresolved
    secret, a bad config, a network error or a timeout all come back as
    `ok: false` with an explanatory message rather than propagating.
    """
    connector = await get_or_404(
        session, Connector, connector_id, organization_id=current_user.organization_id
    )

    async def _record(ok: bool) -> None:
        audit.record(
            session,
            organization_id=current_user.organization_id,
            actor_id=current_user.id,
            action="connector.check",
            target_type="connector",
            target_id=connector.id,
            after={"ok": ok},
            ip=client_ip,
        )
        await session.commit()

    try:
        secret = await resolve_secret(connector.secret_ref)
    except SecretResolutionError as exc:
        # Naming the reference (never the secret) is what makes this fixable.
        await _record(False)
        return ConnectorCheckResult(ok=False, messages=[str(exc)])

    try:
        async with asyncio.timeout(_CHECK_TIMEOUT_SECONDS):
            instance = build_connector(connector.type.value, connector.config, secret)
            try:
                result = await instance.check()
            finally:
                await instance.aclose()
    except Exception as exc:  # a broken connector must never turn into a 500
        await _record(False)
        return ConnectorCheckResult(ok=False, messages=[f"{type(exc).__name__}: {exc}"])

    await _record(result.ok)
    return ConnectorCheckResult(ok=result.ok, messages=result.messages)


def _require_type_licensed(connector_type: DbConnectorType, licence: EffectiveLicense) -> None:
    """A new SharePoint connector is a Business feature; existing ones keep working (LIC-33)."""
    if connector_type is DbConnectorType.SHAREPOINT and not has_feature(
        licence, Feature.SHAREPOINT
    ):
        raise LicenceFeatureError(feature_refusal(Feature.SHAREPOINT))
