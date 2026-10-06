"""SCIM 2.0 endpoints and the organisation's SCIM token (AUTH-3).

`/scim/v2/*` is called by an IdP, not a person: it authenticates with the
organisation's SCIM bearer token only, speaks `application/scim+json`, and
answers errors as SCIM error bodies (the `ScimError` handler in
`api/errors.py`), not problem details. Minting the token is system
administration by a person. Rules: docs/CONTRACTS.md → "### SCIM provisioning".
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse

from app.api.deps import (
    ClientIpDep,
    LicenceDep,
    PersonDep,
    SessionDep,
    SettingsDep,
    enforce_rate_limit,
    require_feature,
    require_superuser,
)
from app.api.errors import RateLimitedError
from app.models import Organization
from app.schemas import BaseSchema
from app.services import scim
from app.services.licensing.features import Feature, has_feature
from app.services.licensing.features import refusal_message as feature_refusal

router = APIRouter(tags=["scim"])

SCIM_PATH = "/api/v1/scim/v2"


class ScimResponse(JSONResponse):
    media_type = scim.SCIM_CONTENT_TYPE


class ScimTokenState(BaseSchema):
    enabled: bool


class ScimTokenRead(BaseSchema):
    """A freshly minted SCIM token, shown once, and the SCIM base path."""

    token: str
    path: str


# --- Token (a person, superuser) ---------------------------------------------------


@router.get(
    "/scim/token",
    response_model=ScimTokenState,
    dependencies=[Depends(require_superuser)],
    summary="Whether SCIM provisioning is on (superuser)",
)
async def scim_token_state(session: SessionDep, current_user: PersonDep) -> ScimTokenState:
    organization = await session.get(Organization, current_user.organization_id)
    return ScimTokenState(enabled=organization is not None and bool(organization.scim_token_hash))


@router.post(
    "/scim/token",
    response_model=ScimTokenRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_superuser), require_feature(Feature.SCIM)],
    summary="Mint (replace) the organisation's SCIM token (superuser)",
)
async def mint_scim_token(
    session: SessionDep, current_user: PersonDep, client_ip: ClientIpDep
) -> ScimTokenRead:
    token = await scim.set_token(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        enabled=True,
        ip=client_ip,
    )
    assert token is not None  # enabled=True always mints
    await session.commit()
    return ScimTokenRead(token=token, path=SCIM_PATH)


@router.delete(
    "/scim/token",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_superuser)],
    summary="Turn SCIM provisioning off (superuser)",
)
async def revoke_scim_token(
    session: SessionDep, current_user: PersonDep, client_ip: ClientIpDep
) -> Response:
    await scim.set_token(
        session,
        organization_id=current_user.organization_id,
        actor_id=current_user.id,
        enabled=False,
        ip=client_ip,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- SCIM (the IdP, bearer token) --------------------------------------------------


async def scim_organization(
    request: Request, session: SessionDep, settings: SettingsDep, licence: LicenceDep
) -> Organization:
    """The organisation whose SCIM token the request carries; SCIM 401 otherwise.

    SCIM 403 while the licence lacks `scim` (LIC-33): what SCIM made stays.
    """
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    organization = (
        await scim.organization_for_token(session, token.strip())
        if scheme.lower() == "bearer" and token.strip()
        else None
    )
    if organization is None:
        raise scim.ScimError(
            401, "A valid SCIM bearer token is required.", headers={"WWW-Authenticate": "Bearer"}
        )
    if not has_feature(licence, Feature.SCIM):
        raise scim.ScimError(403, feature_refusal(Feature.SCIM))
    try:
        await enforce_rate_limit(
            settings, "scim", str(organization.id), settings.rate_limit_api_key_per_minute
        )
    except RateLimitedError as exc:
        raise scim.ScimError(429, exc.detail, headers=exc.headers) from None
    return organization


ScimOrgDep = Annotated[Organization, Depends(scim_organization)]
FilterQuery = Annotated[str | None, Query(alias="filter", max_length=1000)]
# Strings, parsed here: a malformed number must be a SCIM 400, not a 422 problem.
StartQuery = Annotated[str | None, Query(alias="startIndex", max_length=20)]
CountQuery = Annotated[str | None, Query(alias="count", max_length=20)]
ExcludedQuery = Annotated[str | None, Query(alias="excludedAttributes", max_length=200)]


def _window(start_index: str | None, count: str | None) -> tuple[int, int]:
    try:
        return scim.page_window(
            int(start_index) if start_index else None, int(count) if count else None
        )
    except ValueError:
        raise scim.ScimError(
            400, "startIndex and count must be integers.", "invalidValue"
        ) from None


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/") + SCIM_PATH


async def _body(request: Request) -> dict[str, Any]:
    """The JSON object in the request, capped at 1 MiB while it streams in."""
    limit = scim.MAX_SCIM_BODY_BYTES
    too_large = scim.ScimError(413, f"SCIM requests are limited to {limit} bytes.")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_large
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > limit:
            raise too_large
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise scim.ScimError(400, f"The body is not JSON: {exc}", "invalidSyntax") from None
    if not isinstance(body, dict):
        raise scim.ScimError(400, "The body must be a JSON object.", "invalidSyntax")
    return body


def _members_excluded(excluded: str | None) -> bool:
    return excluded is not None and "members" in {
        part.strip().lower() for part in excluded.split(",")
    }


@router.get(
    "/scim/v2/ServiceProviderConfig",
    response_class=ScimResponse,
    summary="SCIM service provider configuration",
)
async def service_provider_config(request: Request, _org: ScimOrgDep) -> dict[str, Any]:
    return scim.service_provider_config(_base_url(request))


@router.get("/scim/v2/ResourceTypes", response_class=ScimResponse, summary="SCIM resource types")
async def resource_types(request: Request, _org: ScimOrgDep) -> dict[str, Any]:
    types = scim.resource_types(_base_url(request))
    return scim.list_response(types, total=len(types), start=1)


@router.get("/scim/v2/Schemas", response_class=ScimResponse, summary="SCIM schemas")
async def schemas(request: Request, _org: ScimOrgDep) -> dict[str, Any]:
    found = scim.schemas(_base_url(request))
    return scim.list_response(found, total=len(found), start=1)


@router.get("/scim/v2/Users", response_class=ScimResponse, summary="List SCIM users")
async def list_users(
    request: Request,
    session: SessionDep,
    organization: ScimOrgDep,
    filter_: FilterQuery = None,
    start_index: StartQuery = None,
    count: CountQuery = None,
) -> dict[str, Any]:
    start, size = _window(start_index, count)
    total, users = await scim.list_users(
        session,
        organization.id,
        filter_=scim.parse_filter(filter_, scim.USER_FILTER_ATTRS),
        start=start,
        count=size,
    )
    resources = await scim.user_resources(session, users, _base_url(request))
    return scim.list_response(resources, total=total, start=start)


@router.post(
    "/scim/v2/Users",
    response_class=ScimResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Provision a SCIM user",
)
async def create_user(
    request: Request, session: SessionDep, organization: ScimOrgDep
) -> dict[str, Any]:
    user = await scim.create_user(session, organization.id, await _body(request))
    await session.commit()
    await session.refresh(user)
    (resource,) = await scim.user_resources(session, [user], _base_url(request))
    return resource


@router.get("/scim/v2/Users/{user_id}", response_class=ScimResponse, summary="Read a SCIM user")
async def get_user(
    user_id: str, request: Request, session: SessionDep, organization: ScimOrgDep
) -> dict[str, Any]:
    user = await scim.get_user(session, organization.id, user_id)
    (resource,) = await scim.user_resources(session, [user], _base_url(request))
    return resource


@router.put("/scim/v2/Users/{user_id}", response_class=ScimResponse, summary="Replace a SCIM user")
async def replace_user(
    user_id: str, request: Request, session: SessionDep, organization: ScimOrgDep
) -> dict[str, Any]:
    user = await scim.get_user(session, organization.id, user_id)
    await scim.replace_user(session, user, await _body(request))
    await session.commit()
    await session.refresh(user)
    (resource,) = await scim.user_resources(session, [user], _base_url(request))
    return resource


@router.patch("/scim/v2/Users/{user_id}", response_class=ScimResponse, summary="Patch a SCIM user")
async def patch_user(
    user_id: str, request: Request, session: SessionDep, organization: ScimOrgDep
) -> dict[str, Any]:
    user = await scim.get_user(session, organization.id, user_id)
    await scim.patch_user(session, user, await _body(request))
    await session.commit()
    await session.refresh(user)
    (resource,) = await scim.user_resources(session, [user], _base_url(request))
    return resource


@router.delete(
    "/scim/v2/Users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deprovision a SCIM user (deactivate and hide)",
)
async def delete_user(
    user_id: str, session: SessionDep, settings: SettingsDep, organization: ScimOrgDep
) -> Response:
    user = await scim.get_user(session, organization.id, user_id)
    await scim.delete_user(session, user, settings)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/scim/v2/Groups", response_class=ScimResponse, summary="List SCIM groups")
async def list_groups(
    request: Request,
    session: SessionDep,
    organization: ScimOrgDep,
    filter_: FilterQuery = None,
    start_index: StartQuery = None,
    count: CountQuery = None,
    excluded: ExcludedQuery = None,
) -> dict[str, Any]:
    start, size = _window(start_index, count)
    total, groups = await scim.list_groups(
        session,
        organization.id,
        filter_=scim.parse_filter(filter_, scim.GROUP_FILTER_ATTRS),
        start=start,
        count=size,
    )
    base = _base_url(request)
    members = not _members_excluded(excluded)
    resources = [
        await scim.group_resource(session, group, base, members=members) for group in groups
    ]
    return scim.list_response(resources, total=total, start=start)


@router.post(
    "/scim/v2/Groups",
    response_class=ScimResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a SCIM group",
)
async def create_group(
    request: Request, session: SessionDep, settings: SettingsDep, organization: ScimOrgDep
) -> dict[str, Any]:
    group = await scim.create_group(session, organization.id, await _body(request), settings)
    await session.commit()
    await session.refresh(group)
    return await scim.group_resource(session, group, _base_url(request))


@router.get("/scim/v2/Groups/{group_id}", response_class=ScimResponse, summary="Read a SCIM group")
async def get_group(
    group_id: str,
    request: Request,
    session: SessionDep,
    organization: ScimOrgDep,
    excluded: ExcludedQuery = None,
) -> dict[str, Any]:
    group = await scim.get_group(session, organization.id, group_id)
    return await scim.group_resource(
        session, group, _base_url(request), members=not _members_excluded(excluded)
    )


@router.put(
    "/scim/v2/Groups/{group_id}", response_class=ScimResponse, summary="Replace a SCIM group"
)
async def replace_group(
    group_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    organization: ScimOrgDep,
) -> dict[str, Any]:
    group = await scim.get_group(session, organization.id, group_id)
    await scim.replace_group(session, group, await _body(request), settings)
    await session.commit()
    await session.refresh(group)
    return await scim.group_resource(session, group, _base_url(request))


@router.patch(
    "/scim/v2/Groups/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Patch a SCIM group",
)
async def patch_group(
    group_id: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    organization: ScimOrgDep,
) -> Response:
    # 204, not the group: a PATCH adding one member to a 10 000-member group
    # would otherwise echo every member back (RFC 7644 §3.5.2 allows either).
    group = await scim.get_group(session, organization.id, group_id)
    await scim.patch_group(session, group, await _body(request), settings)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/scim/v2/Groups/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a SCIM group",
)
async def delete_group(
    group_id: str, session: SessionDep, settings: SettingsDep, organization: ScimOrgDep
) -> Response:
    group = await scim.get_group(session, organization.id, group_id)
    await scim.delete_group(session, group, settings)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
