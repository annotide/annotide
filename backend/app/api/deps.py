"""Shared FastAPI dependencies.

Everything a router needs to know about *who is calling* and *what they may do*
is resolved here, so routers stay thin and the authorisation rules live in one
place.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import (
    ForbiddenError,
    LicenceFeatureError,
    LicenceRestrictedError,
    MfaSetupRequiredError,
    RateLimitedError,
    UnauthorizedError,
)
from app.core.config import Settings, get_settings
from app.core.rate_limit_headers import STATE_KEY as RATE_LIMIT_STATE_KEY
from app.core.security import decode_token
from app.db.session import get_session
from app.models import ApiKey, User
from app.services import api_keys, rate_limit
from app.services.licensing.features import Feature, has_feature
from app.services.licensing.features import refusal_message as feature_refusal
from app.services.licensing.hosts import normalize_host
from app.services.licensing.state import (
    EffectiveLicense,
    effective_license,
    grace_ends,
    is_restricted,
)
from app.services.oidc import OidcClient, get_oidc_client
from app.services.pagination import clamp_limit
from app.services.queue import JobQueue, get_job_queue

# ``auto_error=False`` so a missing header produces our own problem-details
# response rather than FastAPI's default body.
_bearer = HTTPBearer(auto_error=False)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


async def request_host(request: Request) -> str | None:
    """The host the caller reached the install on, for host binding (LIC-29).

    `X-Forwarded-Host` is honoured only from `APP_TRUSTED_PROXIES`:
    `ProxyHeadersMiddleware` has already copied it into `Host` by then.
    """
    return normalize_host(request.headers.get("host"))


RequestHostDep = Annotated[str | None, Depends(request_host)]


async def get_effective_license(
    session: SessionDep, settings: SettingsDep, host: RequestHostDep
) -> EffectiveLicense:
    """The licence in force (LIC-26), read-only: it never advances the clock guard."""
    return await effective_license(session, settings, host=host)


LicenceDep = Annotated[EffectiveLicense, Depends(get_effective_license)]

#: The job queue (ARC-4). Tests override it with an in-memory fake.
QueueDep = Annotated[JobQueue, Depends(get_job_queue)]


def get_job_queue_factory(request: Request) -> Callable[[], Awaitable[JobQueue]]:
    """The queue, resolved only when called, for handlers that enqueue only sometimes.

    A plain `QueueDep` connects to Redis before the handler runs, so a route
    that rarely enqueues would fail with 503 whenever Redis is down. This
    honours an override of `get_job_queue`, as tests install.
    """
    provider: Callable[[], Awaitable[JobQueue]] = request.app.dependency_overrides.get(
        get_job_queue, get_job_queue
    )
    return provider


#: A lazily resolved job queue; see :func:`get_job_queue_factory`.
QueueFactoryDep = Annotated[Callable[[], Awaitable[JobQueue]], Depends(get_job_queue_factory)]
#: The identity provider client (AUTH-1). Tests override it with a mock transport.
OidcClientDep = Annotated[OidcClient, Depends(get_oidc_client)]


@dataclass(frozen=True, slots=True)
class CurrentUser:
    """The authenticated caller, as carried by the access token.

    ``is_service`` marks an API key or service account (AUTH-4) rather than a
    person; some endpoints treat the two differently.
    """

    id: UUID
    organization_id: UUID
    email: str | None
    is_superuser: bool
    is_service: bool
    scopes: frozenset[str]


async def enforce_rate_limit(
    settings: Settings,
    scope: str,
    identity: str,
    limit: int,
    *,
    request: Request | None = None,
) -> None:
    """Count one request against `scope:identity`; 429 over the limit (API-5).

    With `request`, the window is recorded for the `RateLimit-*` response
    headers (`core/rate_limit_headers.py`); the tightest one wins when a
    request is counted more than once.
    """
    if not settings.rate_limit_enabled:
        return
    decision = await rate_limit.get_rate_limiter().hit(scope, identity, limit)
    window = (decision.limit, max(decision.remaining, 0), decision.reset_after)
    if request is not None:
        current = getattr(request.state, RATE_LIMIT_STATE_KEY, None)
        if current is None or window[1] < current[1]:
            setattr(request.state, RATE_LIMIT_STATE_KEY, window)
    if not decision.allowed:
        raise RateLimitedError(
            f"Rate limit of {limit} requests per minute exceeded; retry in "
            f"{decision.reset_after} s.",
            retry_after=decision.reset_after,
            limit=decision.limit,
        )


#: What an `mfa_setup` token may reach: the MFA routes and the caller's profile.
_MFA_SETUP_PREFIX = "/api/v1/auth/mfa"
_MFA_SETUP_PATHS = frozenset({("GET", "/api/v1/auth/me")})


def _allowed_during_mfa_setup(request: Request) -> bool:
    path = request.url.path.rstrip("/")
    if path == _MFA_SETUP_PREFIX or path.startswith(_MFA_SETUP_PREFIX + "/"):
        return True
    return (request.method, path) in _MFA_SETUP_PATHS


async def get_current_user(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: SessionDep,
    settings: SettingsDep,
) -> CurrentUser:
    """Decode the bearer token into a :class:`CurrentUser`.

    A login token is self-contained: no database round trip on the hot path,
    revocation by short TTL. An API key (a token with a ``kid`` claim, AUTH-4)
    is the opposite: its row is looked up on every request so revocation and
    expiry are immediate, and its scopes gate the HTTP method here.
    """
    if credentials is None:
        raise UnauthorizedError("Provide an access token in the Authorization header.")

    if api_keys.is_opaque_token(credentials.credentials):
        key, key_user = await api_keys.resolve_api_key_token(session, credentials.credentials)
        caller = _key_caller(key, key_user, request.method)
        await enforce_rate_limit(
            settings, "key", str(key.id), settings.rate_limit_api_key_per_minute, request=request
        )
        return caller

    try:
        claims = decode_token(credentials.credentials)
    except Exception as exc:
        raise UnauthorizedError("The access token is invalid or has expired.") from exc

    if "kid" in claims:
        caller = await _api_key_caller(session, claims, request.method)
        # Per key, not per user: one runaway integration must not lock the
        # person (or the service account's other keys) out.
        await enforce_rate_limit(
            settings,
            "key",
            str(claims["kid"]),
            settings.rate_limit_api_key_per_minute,
            request=request,
        )
        return caller

    try:
        user = CurrentUser(
            id=UUID(str(claims["sub"])),
            organization_id=UUID(str(claims["org"])),
            email=claims.get("email"),
            is_superuser=bool(claims.get("superuser", False)),
            is_service=bool(claims.get("service", False)),
            scopes=frozenset(claims.get("scopes", [])),
        )
    except (KeyError, ValueError) as exc:
        raise UnauthorizedError("The access token is missing required claims.") from exc
    if claims.get("mfa_setup") and not _allowed_during_mfa_setup(request):
        raise MfaSetupRequiredError(
            "Administrators must use an authenticator app: set it up, then sign in again."
        )
    await enforce_rate_limit(
        settings, "user", str(user.id), settings.rate_limit_per_minute, request=request
    )
    return user


async def _api_key_caller(
    session: AsyncSession, claims: dict[str, Any], method: str
) -> CurrentUser:
    try:
        key_id = UUID(str(claims["kid"]))
        user_id = UUID(str(claims["sub"]))
    except (KeyError, ValueError) as exc:
        raise UnauthorizedError("The API key is missing required claims.") from exc

    key, user = await api_keys.resolve_api_key(session, key_id=key_id, user_id=user_id)
    return _key_caller(key, user, method)


def _key_caller(key: ApiKey, user: User, method: str) -> CurrentUser:
    """The caller an API key acts as, after its scopes allow ``method``."""
    scopes = api_keys.effective_scopes(key.scopes)
    if not api_keys.scope_allows_method(scopes, method):
        raise ForbiddenError("This API key has no write scope.")
    return CurrentUser(
        id=user.id,
        organization_id=user.organization_id,
        email=user.email,
        is_superuser=user.is_superuser,
        is_service=True,
        scopes=scopes,
    )


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


async def require_unrestricted_licence(_user: CurrentUserDep, licence: LicenceDep) -> None:
    """Refuse annotation work and new tasks in restricted mode (LIC-5).

    Put on the routes that do that work; reading, export, snapshots and
    settings never carry it, so data is never held hostage. Authentication
    runs first, so an anonymous caller learns nothing about the licence.
    """
    if is_restricted(licence) and licence.license is not None:
        raise LicenceRestrictedError(
            f"The licence expired on {licence.license.expires_at.isoformat()} and its grace "
            f"period ended on {grace_ends(licence)}. Annotation and new tasks are paused "
            "until an administrator installs a renewed key; reading and export still work."
        )


#: ``dependencies=[UNRESTRICTED_LICENCE]`` on a route decorator.
UNRESTRICTED_LICENCE = Depends(require_unrestricted_licence)


def require_feature(feature: Feature) -> Any:
    """``dependencies=[require_feature(Feature.X)]``: 403 `license-feature` without it (LIC-33).

    Authentication runs first, so an anonymous caller learns nothing about
    the licence.
    """

    async def _check(_user: CurrentUserDep, licence: LicenceDep) -> None:
        if not has_feature(licence, feature):
            raise LicenceFeatureError(feature_refusal(feature))

    return Depends(_check)


async def require_superuser(user: CurrentUserDep) -> CurrentUser:
    """Guard for system-administration endpoints (connectors, IdP, users).

    An API key reaches these only with the `admin` scope on top of a
    superuser owner (AUTH-4).
    """
    if not user.is_superuser:
        raise ForbiddenError("This endpoint requires system administrator rights.")
    if user.is_service and "admin" not in user.scopes:
        raise ForbiddenError("This API key has no admin scope.")
    return user


async def require_person(user: CurrentUserDep) -> CurrentUser:
    """Guard for credential management: an API key may never mint or revoke keys."""
    if user.is_service:
        raise ForbiddenError("API keys and service accounts cannot manage credentials.")
    return user


PersonDep = Annotated[CurrentUser, Depends(require_person)]


SuperuserDep = Annotated[CurrentUser, Depends(require_superuser)]


@dataclass(frozen=True, slots=True)
class PageParams:
    """Validated cursor-pagination query parameters (API-2)."""

    limit: int
    cursor: str | None


async def page_params(
    limit: Annotated[int | None, Query(ge=1, le=200, description="Page size")] = None,
    cursor: Annotated[str | None, Query(description="Opaque cursor from the previous page")] = None,
) -> PageParams:
    """Parse and clamp the pagination query string."""
    return PageParams(limit=clamp_limit(limit), cursor=cursor)


PageParamsDep = Annotated[PageParams, Depends(page_params)]


async def idempotency_key(
    key: Annotated[
        str | None,
        Header(alias="Idempotency-Key", description="Deduplicates retried creates"),
    ] = None,
) -> str | None:
    """Return the caller's idempotency key, if any (API-2)."""
    return key


IdempotencyKeyDep = Annotated[str | None, Depends(idempotency_key)]


async def client_ip(request: Request) -> str | None:
    """The caller's address for the audit log (SEC-3).

    `request.client` is the direct peer unless the deployment lists it in
    `APP_TRUSTED_PROXIES`, in which case `ProxyHeadersMiddleware` has already
    replaced it with the address from `X-Forwarded-For`. The header is never
    read here directly: trusting it unconditionally would let any caller
    forge the audited address.
    """
    return request.client.host if request.client else None


ClientIpDep = Annotated[str | None, Depends(client_ip)]
