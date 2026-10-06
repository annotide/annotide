"""API keys and service accounts (AUTH-4).

A key is an opaque ``ant_<prefix>_<secret>`` token. The row keeps the prefix
(to find it) and a sha256 of the token (to check it), never the token, and
is the authority: every request resolves it, so revoking the row, letting it
expire or deactivating its user cuts access at once. Nothing about a key
depends on ``APP_SECRET_KEY``, so rotating that never breaks integrations.
Keys issued before migration 0032 are JWTs whose ``kid`` claim names the row;
they resolve through :func:`resolve_api_key` until revoked or expired.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import ForbiddenError, NotFoundError, UnauthorizedError, ValidationFailedError
from app.models import ApiKey, ApiKeyScope, User
from app.services import audit

#: Scope levels, each implying the ones before it (CONTRACTS.md → api_key).
_SCOPE_ORDER: tuple[str, ...] = (ApiKeyScope.READ, ApiKeyScope.WRITE, ApiKeyScope.ADMIN)
#: How often `last_used_at` is written; one UPDATE per key per minute at most.
LAST_USED_REFRESH = timedelta(seconds=60)
#: Every opaque key starts with this, so secret scanners can recognise a leak.
TOKEN_PREFIX = "ant_"

#: Methods a `read`-only key may use.
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _aware(stamp: datetime) -> datetime:
    """SQLite hands back naive stamps; treat them as UTC like `services/stats.py`."""
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def new_token() -> tuple[str, str, str]:
    """A fresh key: ``(token, prefix, sha256 hex of token)``. Shown once."""
    prefix = secrets.token_hex(4)
    token = f"{TOKEN_PREFIX}{prefix}_{secrets.token_urlsafe(32)}"
    return token, prefix, hash_token(token)


def hash_token(token: str) -> str:
    """The stored form of a key. A plain sha256 suffices: the token carries
    256 random bits, so there is nothing to brute-force."""
    return hashlib.sha256(token.encode()).hexdigest()


def is_opaque_token(token: str) -> bool:
    return token.startswith(TOKEN_PREFIX)


def effective_scopes(scopes: list[str] | frozenset[str]) -> frozenset[str]:
    """Expand a scope list so `admin` implies `write` implies `read`."""
    highest = -1
    for scope in scopes:
        if scope in _SCOPE_ORDER:
            highest = max(highest, _SCOPE_ORDER.index(scope))
    return frozenset(_SCOPE_ORDER[: highest + 1])


def scope_allows_method(scopes: frozenset[str], method: str) -> bool:
    """Whether a key with ``scopes`` may issue an HTTP ``method``."""
    if method.upper() in _READ_METHODS:
        return ApiKeyScope.READ in scopes
    return ApiKeyScope.WRITE in scopes


def _validate_scopes(scopes: list[str]) -> list[str]:
    unknown = sorted(set(scopes) - set(_SCOPE_ORDER))
    if unknown:
        raise ValidationFailedError(
            f"Unknown scope(s) {', '.join(unknown)}; use read, write or admin."
        )
    return [scope for scope in _SCOPE_ORDER if scope in effective_scopes(scopes)]


async def create_service_account(
    session: AsyncSession,
    *,
    organization_id: UUID,
    display_name: str,
    actor_id: UUID,
    ip: str | None = None,
) -> User:
    """Add a service account: a user row that can only act through API keys.

    The e-mail is synthetic under the reserved `.invalid` TLD so it can never
    collide with, or be mistaken for, a person's address (LIC-16 keeps such
    addresses out of telemetry anyway).
    """
    user = User(
        organization_id=organization_id,
        email=f"svc-{uuid4().hex[:12]}@service.invalid",
        display_name=display_name,
        is_active=True,
        is_superuser=False,
        is_service=True,
    )
    session.add(user)
    await session.flush()
    audit.record(
        session,
        organization_id=organization_id,
        actor_id=actor_id,
        action="service_account.create",
        target_type="user",
        target_id=user.id,
        after={"display_name": display_name},
        ip=ip,
    )
    return user


async def list_service_accounts(session: AsyncSession, *, organization_id: UUID) -> list[User]:
    """Every service account of the organisation, active or not, oldest first."""
    result = await session.scalars(
        select(User)
        .where(User.organization_id == organization_id, User.is_service.is_(True))
        .order_by(User.created_at, User.id)
    )
    return list(result)


async def delete_service_account(
    session: AsyncSession,
    *,
    organization_id: UUID,
    user_id: UUID,
    actor_id: UUID,
    ip: str | None = None,
) -> None:
    """Deactivate a service account and revoke every key it holds.

    The row is kept (audit rows and annotations reference it); deactivation
    is what the request path checks, so access ends with this commit.
    """
    user = await session.get(User, user_id)
    if user is None or user.organization_id != organization_id or not user.is_service:
        raise NotFoundError(f"Service account {user_id} does not exist.")
    now = datetime.now(UTC)
    user.is_active = False
    await session.execute(
        update(ApiKey)
        .where(ApiKey.user_id == user_id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    audit.record(
        session,
        organization_id=organization_id,
        actor_id=actor_id,
        action="service_account.delete",
        target_type="user",
        target_id=user_id,
        ip=ip,
    )


async def create_api_key(
    session: AsyncSession,
    *,
    organization_id: UUID,
    user_id: UUID,
    name: str,
    scopes: list[str],
    expires_at: datetime | None,
    actor_id: UUID,
    ip: str | None = None,
) -> tuple[ApiKey, str]:
    """Mint a key for ``user_id`` and return the row with its one-time token."""
    user = await session.get(User, user_id)
    if user is None or user.organization_id != organization_id:
        raise NotFoundError(f"User {user_id} does not exist.")
    if not user.is_active:
        raise ValidationFailedError("Cannot issue a key for a deactivated user.")
    if expires_at is not None and expires_at <= datetime.now(UTC):
        raise ValidationFailedError("expires_at must be in the future.")

    token, prefix, digest = new_token()
    key = ApiKey(
        organization_id=organization_id,
        user_id=user_id,
        name=name,
        scopes=_validate_scopes(scopes),
        token_prefix=prefix,
        token_hash=digest,
        expires_at=expires_at,
        created_by=actor_id,
    )
    session.add(key)
    await session.flush()
    audit.record(
        session,
        organization_id=organization_id,
        actor_id=actor_id,
        action="api_key.create",
        target_type="api_key",
        target_id=key.id,
        after={
            "user_id": str(user_id),
            "name": name,
            "scopes": key.scopes,
            "expires_at": expires_at.isoformat() if expires_at else None,
        },
        ip=ip,
    )
    return key, token


async def list_api_keys(
    session: AsyncSession, *, organization_id: UUID, user_id: UUID
) -> list[ApiKey]:
    """A user's keys, revoked ones included, newest first."""
    result = await session.scalars(
        select(ApiKey)
        .where(ApiKey.organization_id == organization_id, ApiKey.user_id == user_id)
        .order_by(ApiKey.created_at.desc(), ApiKey.id)
    )
    return list(result)


async def revoke_api_key(
    session: AsyncSession,
    *,
    organization_id: UUID,
    key_id: UUID,
    actor_id: UUID,
    actor_is_superuser: bool,
    ip: str | None = None,
) -> None:
    """Revoke a key. Own keys always; others' only for a superuser."""
    key = await session.get(ApiKey, key_id)
    if key is None or key.organization_id != organization_id:
        raise NotFoundError(f"API key {key_id} does not exist.")
    if key.user_id != actor_id and not actor_is_superuser:
        raise ForbiddenError("Only a system administrator can revoke another user's key.")
    if key.revoked_at is not None:
        return
    key.revoked_at = datetime.now(UTC)
    audit.record(
        session,
        organization_id=organization_id,
        actor_id=actor_id,
        action="api_key.revoke",
        target_type="api_key",
        target_id=key.id,
        after={"user_id": str(key.user_id), "name": key.name},
        ip=ip,
    )


async def resolve_api_key(
    session: AsyncSession, *, key_id: UUID, user_id: UUID
) -> tuple[ApiKey, User]:
    """Look up a `kid` token's row and user, enforcing revocation and expiry.

    Raises :class:`UnauthorizedError` — never a 403 — so a dead key is
    indistinguishable from a bad one. Also stamps ``last_used_at`` at most
    once per :data:`LAST_USED_REFRESH` and commits that on its own.
    """
    row = (
        await session.execute(
            select(ApiKey, User).join(User, User.id == ApiKey.user_id).where(ApiKey.id == key_id)
        )
    ).first()
    if row is None:
        raise UnauthorizedError("The API key is invalid or has been revoked.")
    key, user = row._tuple()
    if key.user_id != user_id:
        raise UnauthorizedError("The API key is invalid or has been revoked.")
    return await _live(session, key, user)


async def resolve_api_key_token(session: AsyncSession, token: str) -> tuple[ApiKey, User]:
    """Look up an opaque ``ant_…`` key by its prefix and check its hash.

    Same rules and errors as :func:`resolve_api_key`; the hash comparison is
    constant-time, and a malformed token is just an invalid one.
    """
    parts = token.split("_", 2)
    if len(parts) != 3 or not parts[1]:
        raise UnauthorizedError("The API key is invalid or has been revoked.")
    row = (
        await session.execute(
            select(ApiKey, User)
            .join(User, User.id == ApiKey.user_id)
            .where(ApiKey.token_prefix == parts[1])
        )
    ).first()
    if row is None:
        raise UnauthorizedError("The API key is invalid or has been revoked.")
    key, user = row._tuple()
    if key.token_hash is None or not hmac.compare_digest(key.token_hash, hash_token(token)):
        raise UnauthorizedError("The API key is invalid or has been revoked.")
    return await _live(session, key, user)


async def _live(session: AsyncSession, key: ApiKey, user: User) -> tuple[ApiKey, User]:
    """Refuse a revoked, expired or orphaned key; stamp `last_used_at`."""
    now = datetime.now(UTC)
    if (
        key.revoked_at is not None
        or (key.expires_at is not None and _aware(key.expires_at) <= now)
        or not user.is_active
    ):
        raise UnauthorizedError("The API key is invalid or has been revoked.")

    if key.last_used_at is None or _aware(key.last_used_at) <= now - LAST_USED_REFRESH:
        await session.execute(update(ApiKey).where(ApiKey.id == key.id).values(last_used_at=now))
        await session.commit()
    return key, user
