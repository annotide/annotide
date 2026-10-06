"""SCIM 2.0 provisioning (AUTH-3, RFC 7643 / RFC 7644).

An IdP — Entra ID, Okta, … — creates, updates and deactivates accounts and
pushes groups before anyone signs in. SCIM groups feed the same group-to-role
mapping as the ID-token claim (`services/group_sync.py`). Rules:
docs/CONTRACTS.md → "### SCIM provisioning (AUTH-3)".

The wire format is loose JSON on purpose: IdPs differ in capitalisation, send
attributes nobody asked for and `"False"` for `false`, so bodies are read as
plain dicts and only the attributes the platform maps are looked at.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import Organization, ScimGroup, ScimGroupMember, User
from app.services import audit, group_sync

SCIM_CONTENT_TYPE: Final = "application/scim+json"
MAX_SCIM_BODY_BYTES: Final = 1024 * 1024
DEFAULT_COUNT: Final = 100
MAX_COUNT: Final = 200

SCHEMA_USER: Final = "urn:ietf:params:scim:schemas:core:2.0:User"
SCHEMA_GROUP: Final = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCHEMA_LIST: Final = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCHEMA_PATCH: Final = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SCHEMA_ERROR: Final = "urn:ietf:params:scim:api:messages:2.0:Error"
SCHEMA_SPC: Final = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
SCHEMA_RESOURCE_TYPE: Final = "urn:ietf:params:scim:schemas:core:2.0:ResourceType"
SCHEMA_SCHEMA: Final = "urn:ietf:params:scim:schemas:core:2.0:Schema"

TOKEN_PREFIX: Final = "scim_"


class ScimError(Exception):
    """A SCIM error response (RFC 7644 §3.12)."""

    def __init__(
        self,
        status: int,
        detail: str,
        scim_type: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type
        self.headers = dict(headers or {})

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schemas": [SCHEMA_ERROR],
            "status": str(self.status),
            "detail": self.detail,
        }
        if self.scim_type:
            body["scimType"] = self.scim_type
        return body


def _not_found(kind: str, resource_id: str) -> ScimError:
    return ScimError(404, f"{kind} {resource_id} not found")


# --- Token ---------------------------------------------------------------------


def new_scim_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_scim_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def organization_for_token(session: AsyncSession, token: str) -> Organization | None:
    """The organisation whose SCIM token this is, or None."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    organization: Organization | None = await session.scalar(
        select(Organization).where(Organization.scim_token_hash == hash_scim_token(token))
    )
    return organization


async def set_token(
    session: AsyncSession,
    *,
    organization_id: UUID,
    actor_id: UUID,
    enabled: bool,
    ip: str | None,
) -> str | None:
    """Mint (replace) the organisation's token, or remove it. The caller commits."""
    organization = await session.get(Organization, organization_id)
    assert organization is not None  # the caller's own organisation
    token = new_scim_token() if enabled else None
    organization.scim_token_hash = hash_scim_token(token) if token else None
    audit.record(
        session,
        organization_id=organization_id,
        actor_id=actor_id,
        action="organization.scim_token",
        target_type="organization",
        target_id=organization_id,
        after={"enabled": enabled},
        ip=ip,
    )
    return token


# --- Request parsing -------------------------------------------------------------

_FILTER_RE = re.compile(r'^\s*([A-Za-z][\w.:]*)\s+eq\s+"((?:[^"\\]|\\.)*)"\s*$', re.IGNORECASE)
_MEMBER_PATH_RE = re.compile(r'^members\[\s*value\s+eq\s+"([^"]*)"\s*\]$', re.IGNORECASE)
_CORE_PREFIXES = (SCHEMA_USER.lower() + ":", SCHEMA_GROUP.lower() + ":")


@dataclass(frozen=True, slots=True)
class Filter:
    """One `<attr> eq "<value>"` comparison; `attr` is lower-case."""

    attr: str
    value: str


def parse_filter(text: str | None, allowed: Iterable[str]) -> Filter | None:
    """The one comparison this implementation supports, or 400 `invalidFilter`."""
    if text is None or not text.strip():
        return None
    match = _FILTER_RE.match(text)
    attr = _attr(match.group(1)) if match else ""
    if not match or attr not in allowed:
        raise ScimError(
            400,
            f'Unsupported filter {text!r}: use one of {", ".join(allowed)} eq "<value>".',
            "invalidFilter",
        )
    return Filter(attr, re.sub(r"\\(.)", r"\1", match.group(2)))


def page_window(start_index: int | None, count: int | None) -> tuple[int, int]:
    """(`startIndex`, `count`) clamped per RFC 7644 §3.4.2.4."""
    start = max(1, start_index or 1)
    size = DEFAULT_COUNT if count is None else max(0, min(count, MAX_COUNT))
    return start, size


def _attr(name: str) -> str:
    """A SCIM attribute path, lower-cased, without the core schema URN."""
    lowered = name.strip().lower()
    for prefix in _CORE_PREFIXES:
        if lowered.startswith(prefix):
            return lowered[len(prefix) :]
    return lowered


def _operations(body: Mapping[str, Any]) -> list[tuple[str, str | None, Any]]:
    """PATCH operations as (op, lower-cased path or None, value)."""
    raw = body.get("Operations", body.get("operations"))
    if not isinstance(raw, list) or not raw:
        raise ScimError(400, "A PATCH needs a non-empty Operations list.", "invalidSyntax")
    out: list[tuple[str, str | None, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ScimError(400, "Each operation must be an object.", "invalidSyntax")
        op = str(item.get("op", "")).lower()
        if op not in {"add", "replace", "remove"}:
            raise ScimError(400, f"Unsupported PATCH op {item.get('op')!r}.", "invalidSyntax")
        path = item.get("path")
        if path is not None and not isinstance(path, str):
            raise ScimError(400, "path must be a string.", "invalidPath")
        out.append((op, _attr(path) if path else None, item.get("value")))
    return out


def _string(value: Any, name: str, *, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise ScimError(400, f"{name} is required.", "invalidValue")
        return None
    if not isinstance(value, str):
        raise ScimError(400, f"{name} must be a string.", "invalidValue")
    value = value.strip()
    if required and not value:
        raise ScimError(400, f"{name} must not be empty.", "invalidValue")
    if len(value) > 255:
        raise ScimError(400, f"{name} is longer than 255 characters.", "invalidValue")
    return value or None


def _bool(value: Any, name: str) -> bool:
    """A SCIM boolean; Entra has been known to send the strings "True" / "False"."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ScimError(400, f"{name} must be a boolean.", "invalidValue")


def _email(value: Any) -> str:
    email = _string(value, "userName", required=True)
    assert email is not None  # required above
    if "@" not in email or any(ch.isspace() for ch in email):
        raise ScimError(400, "userName must be the user's e-mail address.", "invalidValue")
    return email


def _uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _ids(values: Iterable[str]) -> set[UUID]:
    """The values that are UUIDs; anything else cannot name a member."""
    return {parsed for parsed in map(_uuid, values) if parsed is not None}


def _stamp(moment: datetime) -> str:
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.isoformat()


def list_response(resources: Sequence[dict[str, Any]], *, total: int, start: int) -> dict[str, Any]:
    return {
        "schemas": [SCHEMA_LIST],
        "totalResults": total,
        "startIndex": start,
        "itemsPerPage": len(resources),
        "Resources": list(resources),
    }


# --- Users -----------------------------------------------------------------------

USER_FILTER_ATTRS: Final = ("username", "externalid", "id", "emails.value")


@dataclass(slots=True)
class _UserChanges:
    """Attributes a request sets; None means "not in this request"."""

    email: str | None = None
    display_name: str | None = None
    external_id: str | None = None
    clear_external_id: bool = False
    active: bool | None = None


def _visible_users(organization_id: UUID) -> list[ColumnElement[bool]]:
    return [
        User.organization_id == organization_id,
        User.is_service.is_(False),
        User.erased_at.is_(None),
        User.scim_deleted_at.is_(None),
    ]


async def _groups_of(
    session: AsyncSession, user_ids: Sequence[UUID]
) -> dict[UUID, list[ScimGroup]]:
    if not user_ids:
        return {}
    rows = await session.execute(
        select(ScimGroupMember.user_id, ScimGroup)
        .join(ScimGroup, ScimGroup.id == ScimGroupMember.group_id)
        .where(ScimGroupMember.user_id.in_(user_ids))
        .order_by(ScimGroup.display_name)
    )
    out: dict[UUID, list[ScimGroup]] = {}
    for user_id, group in rows.all():
        out.setdefault(user_id, []).append(group)
    return out


def user_resource(user: User, groups: Sequence[ScimGroup], base_url: str) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "schemas": [SCHEMA_USER],
        "id": str(user.id),
        "userName": user.email,
        "name": {"formatted": user.display_name},
        "displayName": user.display_name,
        "emails": [{"value": user.email, "type": "work", "primary": True}],
        "active": user.is_active,
        "groups": [
            {
                "value": str(group.id),
                "display": group.display_name,
                "$ref": f"{base_url}/Groups/{group.id}",
            }
            for group in groups
        ],
        "meta": {
            "resourceType": "User",
            "created": _stamp(user.created_at),
            "lastModified": _stamp(user.updated_at),
            "location": f"{base_url}/Users/{user.id}",
        },
    }
    if user.scim_external_id is not None:
        resource["externalId"] = user.scim_external_id
    return resource


async def user_resources(
    session: AsyncSession, users: Sequence[User], base_url: str
) -> list[dict[str, Any]]:
    groups = await _groups_of(session, [user.id for user in users])
    return [user_resource(user, groups.get(user.id, []), base_url) for user in users]


async def list_users(
    session: AsyncSession,
    organization_id: UUID,
    *,
    filter_: Filter | None,
    start: int,
    count: int,
) -> tuple[int, list[User]]:
    conditions = _visible_users(organization_id)
    if filter_ is not None:
        if filter_.attr in {"username", "emails.value"}:
            conditions.append(User.email == filter_.value)
        elif filter_.attr == "externalid":
            conditions.append(User.scim_external_id == filter_.value)
        else:
            user_id = _uuid(filter_.value)
            if user_id is None:
                return 0, []
            conditions.append(User.id == user_id)
    total = await session.scalar(select(func.count()).select_from(User).where(*conditions))
    if count == 0:
        return int(total or 0), []
    users = (
        await session.scalars(
            select(User)
            .where(*conditions)
            .order_by(User.created_at, User.id)
            .offset(start - 1)
            .limit(count)
        )
    ).all()
    return int(total or 0), list(users)


async def get_user(session: AsyncSession, organization_id: UUID, user_id: str) -> User:
    parsed = _uuid(user_id)
    user: User | None = (
        await session.scalar(
            select(User).where(User.id == parsed, *_visible_users(organization_id))
        )
        if parsed is not None
        else None
    )
    if user is None:
        raise _not_found("User", user_id)
    return user


def _display_name(body: Mapping[str, Any], email: str) -> str:
    display = _string(body.get("displayName"), "displayName")
    name = body.get("name")
    if display is None and isinstance(name, dict):
        display = _string(name.get("formatted"), "name.formatted")
        if display is None:
            parts = [_string(name.get(key), f"name.{key}") for key in ("givenName", "familyName")]
            display = " ".join(part for part in parts if part) or None
    return display or email.split("@", 1)[0]


def _full_user_changes(body: Mapping[str, Any]) -> _UserChanges:
    """What a POST or PUT body says, every mapped attribute included."""
    email = _email(body.get("userName"))
    external_id = _string(body.get("externalId"), "externalId")
    active = body.get("active")
    return _UserChanges(
        email=email,
        display_name=_display_name(body, email),
        external_id=external_id,
        clear_external_id=external_id is None,
        active=True if active is None else _bool(active, "active"),
    )


def _patch_user_changes(body: Mapping[str, Any]) -> _UserChanges:
    changes = _UserChanges()

    def put(path: str, value: Any) -> None:
        if path == "active":
            changes.active = _bool(value, "active")
        elif path == "username":
            changes.email = _email(value)
        elif path in {"displayname", "name.formatted"}:
            changes.display_name = _string(value, "displayName", required=True)
        elif path == "name" and isinstance(value, dict) and "formatted" in value:
            changes.display_name = _string(value["formatted"], "name.formatted", required=True)
        elif path == "externalid":
            changes.external_id = _string(value, "externalId")
            changes.clear_external_id = changes.external_id is None
        # Anything else (emails, name.givenName, enterprise fields) is not mapped.

    for op, path, value in _operations(body):
        if path is None:
            if op == "remove" or not isinstance(value, dict):
                raise ScimError(400, "A PATCH without a path needs an object value.", "noTarget")
            for key, item in value.items():
                put(_attr(key), item)
        elif op == "remove":
            if path == "externalid":
                changes.external_id, changes.clear_external_id = None, True
        else:
            put(path, value)
    return changes


async def _email_taken(session: AsyncSession, email: str, *, other_than: UUID | None) -> bool:
    query = select(User.id).where(User.email == email)
    if other_than is not None:
        query = query.where(User.id != other_than)
    return (await session.scalar(query)) is not None


async def _other_active_superusers(session: AsyncSession, user: User) -> int:
    count = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(
            User.organization_id == user.organization_id,
            User.is_superuser.is_(True),
            User.is_active.is_(True),
            User.id != user.id,
        )
    )
    return int(count or 0)


async def _guard_last_superuser(session: AsyncSession, user: User) -> None:
    if user.is_superuser and user.is_active and await _other_active_superusers(session, user) == 0:
        raise ScimError(
            409,
            "This is the organisation's last active administrator; it is not deactivated "
            "over SCIM. Make someone else an administrator first.",
            "mutability",
        )


async def _apply_user(session: AsyncSession, user: User, changes: _UserChanges) -> None:
    """Write `changes` to `user` with its audit rows. The caller commits."""
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    if changes.email is not None and changes.email != user.email:
        if await _email_taken(session, changes.email, other_than=user.id):
            raise ScimError(409, f"{changes.email} is already in use.", "uniqueness")
        before["email"], after["email"] = user.email, changes.email
        user.email = changes.email
    if changes.display_name is not None and changes.display_name != user.display_name:
        before["display_name"], after["display_name"] = user.display_name, changes.display_name
        user.display_name = changes.display_name
    external_id = None if changes.clear_external_id else changes.external_id
    if (changes.clear_external_id or external_id is not None) and (
        external_id != user.scim_external_id
    ):
        before["external_id"], after["external_id"] = user.scim_external_id, external_id
        user.scim_external_id = external_id
    if after:
        _audit_user(session, user, "user.update", before=before, after=after)
    if changes.active is not None and changes.active != user.is_active:
        if not changes.active:
            await _guard_last_superuser(session, user)
        user.is_active = changes.active
        _audit_user(session, user, "user.activate" if changes.active else "user.deactivate")


def _audit_user(
    session: AsyncSession,
    user: User,
    action: str,
    *,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    audit.record(
        session,
        organization_id=user.organization_id,
        actor_id=None,
        action=action,
        target_type="user",
        target_id=user.id,
        before=before,
        after={**(after or {}), "via": "scim"},
    )


async def create_user(
    session: AsyncSession, organization_id: UUID, body: Mapping[str, Any]
) -> User:
    """Provision an account, or revive one a SCIM DELETE hid. The caller commits."""
    changes = _full_user_changes(body)
    assert changes.email is not None  # userName is required
    existing = await session.scalar(select(User).where(User.email == changes.email))
    if existing is not None:
        revivable = (
            existing.organization_id == organization_id
            and existing.scim_deleted_at is not None
            and existing.erased_at is None
            and not existing.is_service
        )
        if not revivable:
            raise ScimError(409, f"{changes.email} is already in use.", "uniqueness")
        existing.scim_deleted_at = None
        await _apply_user(session, existing, changes)
        _audit_user(session, existing, "user.provision", after={"revived": True})
        return existing
    user = User(
        organization_id=organization_id,
        email=changes.email,
        display_name=changes.display_name or changes.email,
        password_hash=None,
        is_active=bool(changes.active),
        is_superuser=False,
        scim_external_id=changes.external_id,
    )
    session.add(user)
    await session.flush()
    _audit_user(session, user, "user.provision", after={"email": user.email, "method": "scim"})
    return user


async def replace_user(session: AsyncSession, user: User, body: Mapping[str, Any]) -> None:
    await _apply_user(session, user, _full_user_changes(body))


async def patch_user(session: AsyncSession, user: User, body: Mapping[str, Any]) -> None:
    await _apply_user(session, user, _patch_user_changes(body))


async def delete_user(session: AsyncSession, user: User, settings: Settings) -> None:
    """Deactivate, leave every SCIM group, and hide from SCIM. Not an erasure (SEC-6)."""
    await _guard_last_superuser(session, user)
    memberships = (
        await session.scalars(select(ScimGroupMember).where(ScimGroupMember.user_id == user.id))
    ).all()
    for membership in memberships:
        await session.delete(membership)
    await session.flush()
    if memberships:
        await resync(session, user.organization_id, [user.id], settings)
    user.is_active = False
    user.scim_deleted_at = datetime.now(UTC)
    _audit_user(session, user, "user.scim_delete")


# --- Groups ----------------------------------------------------------------------

GROUP_FILTER_ATTRS: Final = ("displayname", "externalid", "id")


async def _member_ids(session: AsyncSession, group_id: UUID) -> set[UUID]:
    return set(
        (
            await session.scalars(
                select(ScimGroupMember.user_id).where(ScimGroupMember.group_id == group_id)
            )
        ).all()
    )


async def group_resource(
    session: AsyncSession, group: ScimGroup, base_url: str, *, members: bool = True
) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "schemas": [SCHEMA_GROUP],
        "id": str(group.id),
        "displayName": group.display_name,
        "meta": {
            "resourceType": "Group",
            "created": _stamp(group.created_at),
            "lastModified": _stamp(group.updated_at),
            "location": f"{base_url}/Groups/{group.id}",
        },
    }
    if group.external_id is not None:
        resource["externalId"] = group.external_id
    if members:
        rows = await session.execute(
            select(User.id, User.display_name)
            .join(ScimGroupMember, ScimGroupMember.user_id == User.id)
            .where(ScimGroupMember.group_id == group.id)
            .order_by(User.display_name, User.id)
        )
        resource["members"] = [
            {"value": str(user_id), "display": name, "$ref": f"{base_url}/Users/{user_id}"}
            for user_id, name in rows.all()
        ]
    return resource


async def list_groups(
    session: AsyncSession,
    organization_id: UUID,
    *,
    filter_: Filter | None,
    start: int,
    count: int,
) -> tuple[int, list[ScimGroup]]:
    conditions: list[ColumnElement[bool]] = [ScimGroup.organization_id == organization_id]
    if filter_ is not None:
        if filter_.attr == "displayname":
            conditions.append(ScimGroup.display_name == filter_.value)
        elif filter_.attr == "externalid":
            conditions.append(ScimGroup.external_id == filter_.value)
        else:
            group_id = _uuid(filter_.value)
            if group_id is None:
                return 0, []
            conditions.append(ScimGroup.id == group_id)
    total = await session.scalar(select(func.count()).select_from(ScimGroup).where(*conditions))
    if count == 0:
        return int(total or 0), []
    groups = (
        await session.scalars(
            select(ScimGroup)
            .where(*conditions)
            .order_by(ScimGroup.display_name, ScimGroup.id)
            .offset(start - 1)
            .limit(count)
        )
    ).all()
    return int(total or 0), list(groups)


async def get_group(session: AsyncSession, organization_id: UUID, group_id: str) -> ScimGroup:
    parsed = _uuid(group_id)
    group = await session.get(ScimGroup, parsed) if parsed is not None else None
    if group is None or group.organization_id != organization_id:
        raise _not_found("Group", group_id)
    return group


def _member_values(value: Any) -> list[str]:
    """`[{"value": "<id>"}, …]` (or a single object) → the ids."""
    items = value if isinstance(value, list) else [value]
    out: list[str] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("value"), str):
            raise ScimError(
                400, 'members must be a list of {"value": "<user id>"}.', "invalidValue"
            )
        out.append(item["value"])
    return out


async def _resolve_members(
    session: AsyncSession, organization_id: UUID, values: Iterable[str]
) -> set[UUID]:
    wanted = {value: _uuid(value) for value in values}
    ids = {parsed for parsed in wanted.values() if parsed is not None}
    found = set(
        (
            await session.scalars(
                select(User.id).where(User.id.in_(ids), *_visible_users(organization_id))
            )
        ).all()
        if ids
        else []
    )
    unknown = sorted(value for value, parsed in wanted.items() if parsed not in found)
    if unknown:
        raise ScimError(400, f"Unknown user id(s): {', '.join(unknown[:10])}.", "invalidValue")
    return found


async def _name_taken(
    session: AsyncSession, organization_id: UUID, name: str, *, other_than: UUID | None
) -> bool:
    query = select(ScimGroup.id).where(
        ScimGroup.organization_id == organization_id, ScimGroup.display_name == name
    )
    if other_than is not None:
        query = query.where(ScimGroup.id != other_than)
    return (await session.scalar(query)) is not None


async def _save_group(
    session: AsyncSession,
    group: ScimGroup,
    *,
    display_name: str | None,
    external_id: str | None,
    clear_external_id: bool,
    members: set[UUID] | None,
    settings: Settings,
    action: str,
) -> None:
    """Apply group changes, re-sync the users they affect, audit. The caller commits."""
    renamed = False
    if display_name is not None and display_name != group.display_name:
        if await _name_taken(session, group.organization_id, display_name, other_than=group.id):
            raise ScimError(409, f"A group named {display_name!r} exists.", "uniqueness")
        group.display_name, renamed = display_name, True
    new_external = None if clear_external_id else external_id
    if (clear_external_id or external_id is not None) and new_external != group.external_id:
        group.external_id, renamed = new_external, True
    await session.flush()

    current = await _member_ids(session, group.id)
    target = current if members is None else members
    for user_id in target - current:
        session.add(ScimGroupMember(group_id=group.id, user_id=user_id))
    if current - target:
        rows = await session.scalars(
            select(ScimGroupMember).where(
                ScimGroupMember.group_id == group.id,
                ScimGroupMember.user_id.in_(current - target),
            )
        )
        for row in rows.all():
            await session.delete(row)
    await session.flush()

    affected = (current | target) if renamed else (current ^ target)
    await resync(session, group.organization_id, affected, settings)
    audit.record(
        session,
        organization_id=group.organization_id,
        actor_id=None,
        action=action,
        target_type="scim_group",
        target_id=group.id,
        after={
            "display_name": group.display_name,
            "external_id": group.external_id,
            "members": len(target),
            "via": "scim",
        },
    )


async def create_group(
    session: AsyncSession, organization_id: UUID, body: Mapping[str, Any], settings: Settings
) -> ScimGroup:
    name = _string(body.get("displayName"), "displayName", required=True)
    assert name is not None  # required above
    if await _name_taken(session, organization_id, name, other_than=None):
        raise ScimError(409, f"A group named {name!r} exists.", "uniqueness")
    members = await _resolve_members(
        session, organization_id, _member_values(body.get("members") or [])
    )
    group = ScimGroup(
        organization_id=organization_id,
        display_name=name,
        external_id=_string(body.get("externalId"), "externalId"),
    )
    session.add(group)
    await session.flush()
    await _save_group(
        session,
        group,
        display_name=None,
        external_id=None,
        clear_external_id=False,
        members=members,
        settings=settings,
        action="scim_group.create",
    )
    return group


async def replace_group(
    session: AsyncSession, group: ScimGroup, body: Mapping[str, Any], settings: Settings
) -> None:
    external_id = _string(body.get("externalId"), "externalId")
    await _save_group(
        session,
        group,
        display_name=_string(body.get("displayName"), "displayName", required=True),
        external_id=external_id,
        clear_external_id=external_id is None,
        members=await _resolve_members(
            session, group.organization_id, _member_values(body.get("members") or [])
        ),
        settings=settings,
        action="scim_group.update",
    )


async def patch_group(
    session: AsyncSession, group: ScimGroup, body: Mapping[str, Any], settings: Settings
) -> None:
    display_name: str | None = None
    external_id: str | None = None
    clear_external_id = False
    members = await _member_ids(session, group.id)
    organization_id = group.organization_id

    for op, path, value in _operations(body):
        if path is None:
            if op == "remove" or not isinstance(value, dict):
                raise ScimError(400, "A PATCH without a path needs an object value.", "noTarget")
            items = [(_attr(key), item) for key, item in value.items()]
        else:
            items = [(path, value)]
        for attr, item in items:
            member_match = _MEMBER_PATH_RE.match(attr)
            if attr == "displayname" and op != "remove":
                display_name = _string(item, "displayName", required=True)
            elif attr == "externalid":
                external_id = None if op == "remove" else _string(item, "externalId")
                clear_external_id = external_id is None
            elif attr == "members":
                values = [] if item is None else _member_values(item)
                if op == "remove":
                    members = set() if item is None else members - _ids(values)
                    continue
                resolved = await _resolve_members(session, organization_id, values)
                members = resolved if op == "replace" else members | resolved
            elif member_match and op == "remove":
                members -= _ids([member_match.group(1)])
            # Other attributes are not mapped.

    await _save_group(
        session,
        group,
        display_name=display_name,
        external_id=external_id,
        clear_external_id=clear_external_id,
        members=members,
        settings=settings,
        action="scim_group.update",
    )


async def delete_group(session: AsyncSession, group: ScimGroup, settings: Settings) -> None:
    members = await _member_ids(session, group.id)
    audit.record(
        session,
        organization_id=group.organization_id,
        actor_id=None,
        action="scim_group.delete",
        target_type="scim_group",
        target_id=group.id,
        after={"display_name": group.display_name, "members": len(members), "via": "scim"},
    )
    await session.delete(group)
    await session.flush()
    await resync(session, group.organization_id, members, settings)


# --- Group sync --------------------------------------------------------------------


async def has_groups(session: AsyncSession, organization_id: UUID) -> bool:
    """Whether SCIM is the organisation's source of groups (sign-in sync then stands down)."""
    found = await session.scalar(
        select(ScimGroup.id).where(ScimGroup.organization_id == organization_id).limit(1)
    )
    return found is not None


async def group_keys(session: AsyncSession, user_id: UUID) -> set[str]:
    """The names and external ids of a user's SCIM groups — what `idp_groups` maps."""
    rows = await session.execute(
        select(ScimGroup.display_name, ScimGroup.external_id)
        .join(ScimGroupMember, ScimGroupMember.group_id == ScimGroup.id)
        .where(ScimGroupMember.user_id == user_id)
    )
    keys: set[str] = set()
    for name, external_id in rows.all():
        keys.add(name)
        if external_id:
            keys.add(external_id)
    return keys


async def resync(
    session: AsyncSession, organization_id: UUID, user_ids: Iterable[UUID], settings: Settings
) -> None:
    """Re-run AUTH-3 group sync for `user_ids` from their SCIM groups."""
    admin_groups = group_sync.parse_admin_groups(settings.oidc_admin_groups)
    for user_id in sorted(set(user_ids)):
        user = await session.get(User, user_id)
        if user is None or user.organization_id != organization_id:
            continue
        await group_sync.sync_groups(
            session,
            user,
            await group_keys(session, user_id),
            admin_groups=admin_groups,
            scim=True,
        )


# --- Discovery ---------------------------------------------------------------------


def service_provider_config(base_url: str) -> dict[str, Any]:
    return {
        "schemas": [SCHEMA_SPC],
        "documentationUri": "https://datatracker.ietf.org/doc/html/rfc7644",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": MAX_COUNT},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "Bearer token",
                "description": "The organisation's SCIM token, minted under Users → SCIM.",
                "primary": True,
            }
        ],
        "meta": {"resourceType": "ServiceProviderConfig", "location": f"{base_url}/"},
    }


def resource_types(base_url: str) -> list[dict[str, Any]]:
    return [
        {
            "schemas": [SCHEMA_RESOURCE_TYPE],
            "id": name,
            "name": name,
            "endpoint": f"/{name}s",
            "schema": schema,
            "meta": {
                "resourceType": "ResourceType",
                "location": f"{base_url}/ResourceTypes/{name}",
            },
        }
        for name, schema in (("User", SCHEMA_USER), ("Group", SCHEMA_GROUP))
    ]


def _attribute(
    name: str,
    *,
    kind: str = "string",
    required: bool = False,
    mutability: str = "readWrite",
    uniqueness: str = "none",
    multi: bool = False,
    sub: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    attribute: dict[str, Any] = {
        "name": name,
        "type": kind,
        "multiValued": multi,
        "required": required,
        "caseExact": False,
        "mutability": mutability,
        "returned": "default",
        "uniqueness": uniqueness,
    }
    if sub:
        attribute["subAttributes"] = list(sub)
    return attribute


def schemas(base_url: str) -> list[dict[str, Any]]:
    user_attributes = [
        _attribute("userName", required=True, uniqueness="server"),
        _attribute("displayName"),
        _attribute("name", kind="complex", sub=[_attribute("formatted")]),
        _attribute("active", kind="boolean"),
        _attribute(
            "emails",
            kind="complex",
            multi=True,
            mutability="readOnly",
            sub=[_attribute("value"), _attribute("type"), _attribute("primary", kind="boolean")],
        ),
        _attribute(
            "groups",
            kind="complex",
            multi=True,
            mutability="readOnly",
            sub=[_attribute("value"), _attribute("display")],
        ),
    ]
    group_attributes = [
        _attribute("displayName", required=True, uniqueness="server"),
        _attribute(
            "members",
            kind="complex",
            multi=True,
            sub=[_attribute("value"), _attribute("display", mutability="readOnly")],
        ),
    ]
    return [
        {
            "schemas": [SCHEMA_SCHEMA],
            "id": schema,
            "name": name,
            "attributes": attributes,
            "meta": {"resourceType": "Schema", "location": f"{base_url}/Schemas/{schema}"},
        }
        for schema, name, attributes in (
            (SCHEMA_USER, "User", user_attributes),
            (SCHEMA_GROUP, "Group", group_attributes),
        )
    ]


__all__ = [
    "GROUP_FILTER_ATTRS",
    "MAX_SCIM_BODY_BYTES",
    "SCIM_CONTENT_TYPE",
    "USER_FILTER_ATTRS",
    "Filter",
    "ScimError",
    "create_group",
    "create_user",
    "delete_group",
    "delete_user",
    "get_group",
    "get_user",
    "group_keys",
    "group_resource",
    "has_groups",
    "list_groups",
    "list_response",
    "list_users",
    "organization_for_token",
    "page_window",
    "parse_filter",
    "patch_group",
    "patch_user",
    "replace_group",
    "replace_user",
    "resource_types",
    "resync",
    "schemas",
    "service_provider_config",
    "set_token",
    "user_resources",
]
