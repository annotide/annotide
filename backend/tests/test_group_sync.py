"""Tests for `services/group_sync.py` and the `idp_groups` settings key (AUTH-3)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.models import (
    AuditEvent,
    Membership,
    MembershipSource,
    Organization,
    Project,
    ProjectRole,
    User,
)
from app.schemas import ProjectUpdate
from app.services.group_sync import parse_admin_groups, role_for, sync_groups
from app.services.oidc import identity_from_claims


@compiles(CITEXT, "sqlite")  # pragma: no cover - exercised implicitly by every test
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


_TABLES = [
    cast(Table, model.__table__) for model in (Organization, User, Project, Membership, AuditEvent)
]
ADMINS = frozenset({"admins"})


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    async with async_sessionmaker(bind=engine, expire_on_commit=False)() as db:
        yield db
    await engine.dispose()


async def _org(session: AsyncSession) -> Organization:
    org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:6]}")
    session.add(org)
    await session.flush()
    return org


async def _user(session: AsyncSession, org: Organization, *, superuser: bool = False) -> User:
    user = User(
        organization_id=org.id,
        email=f"{uuid4().hex[:8]}@example.com",
        display_name="U",
        is_active=True,
        is_superuser=superuser,
    )
    session.add(user)
    await session.flush()
    return user


async def _project(
    session: AsyncSession, org: Organization, mapping: dict[str, str] | None
) -> Project:
    settings = {"idp_groups": mapping} if mapping is not None else {}
    project = Project(organization_id=org.id, name="P", workflow={}, settings=settings)
    session.add(project)
    await session.flush()
    return project


async def _memberships(session: AsyncSession, user_id: UUID) -> dict[UUID, Membership]:
    rows = await session.scalars(select(Membership).where(Membership.user_id == user_id))
    return {m.project_id: m for m in rows}


async def _actions(session: AsyncSession) -> list[str]:
    return list(await session.scalars(select(AuditEvent.action)))


# --- pure helpers --------------------------------------------------------------


def test_role_for_picks_the_highest_role() -> None:
    mapping = {"a": "viewer", "b": "owner", "c": "reviewer", "d": "bogus"}
    assert role_for(["a", "c"], mapping) == ProjectRole.REVIEWER
    assert role_for(["a", "b", "c"], mapping) == ProjectRole.OWNER
    assert role_for(["d", "x"], mapping) is None
    assert role_for(["a"], None) is None


def test_parse_admin_groups() -> None:
    assert parse_admin_groups(None) is None
    assert parse_admin_groups("  ") is None
    assert parse_admin_groups("a, b,,c ") == frozenset({"a", "b", "c"})


def test_identity_reads_the_groups_claim() -> None:
    claims = {"sub": "s", "email": "a@example.com", "groups": ["b", "a", "a", 3, ""]}
    assert identity_from_claims(claims, groups_claim="groups").groups == ("a", "b")
    assert identity_from_claims({**claims, "roles": "one"}, groups_claim="roles").groups == ("one",)
    assert identity_from_claims(claims).groups is None  # sync off by default in the helper


def test_an_absent_groups_claim_means_no_groups_unless_it_overflowed() -> None:
    base = {"sub": "s", "email": "a@example.com"}
    # Keycloak and Entra leave the claim out for a user in no group.
    assert identity_from_claims(base, groups_claim="groups").groups == ()
    assert identity_from_claims({**base, "groups": ""}, groups_claim="groups").groups == ()
    # Entra overage: the groups are elsewhere, so sync must not act on them.
    overage = {**base, "_claim_names": {"groups": "src1"}, "_claim_sources": {"src1": {}}}
    assert identity_from_claims(overage, groups_claim="groups").groups is None
    assert identity_from_claims({**base, "hasgroups": True}, groups_claim="groups").groups is None
    # A shape we do not understand is not read as "no groups".
    assert identity_from_claims({**base, "groups": {"a": 1}}, groups_claim="groups").groups is None


def test_idp_groups_setting_is_validated() -> None:
    ok = ProjectUpdate(settings={"idp_groups": {"labelers": "annotator"}, "other": 1})
    assert ok.settings == {"idp_groups": {"labelers": "annotator"}, "other": 1}
    for bad in ({"labelers": "admin"}, ["labelers"], {"": "viewer"}):
        with pytest.raises(ValidationError):
            ProjectUpdate(settings={"idp_groups": bad})


# --- memberships -----------------------------------------------------------------


async def test_grants_updates_and_removes_idp_memberships(session: AsyncSession) -> None:
    org = await _org(session)
    user = await _user(session, org)
    owner = await _user(session, org)
    roads = await _project(session, org, {"labelers": "annotator", "leads": "reviewer"})
    rivers = await _project(session, org, {"labelers": "viewer"})
    unmapped = await _project(session, org, None)
    for project in (roads, rivers):
        session.add(Membership(user_id=owner.id, project_id=project.id, role=ProjectRole.OWNER))

    await sync_groups(session, user, ["labelers", "leads"], admin_groups=None)
    held = await _memberships(session, user.id)
    assert {pid: m.role for pid, m in held.items()} == {
        roads.id: ProjectRole.REVIEWER,
        rivers.id: ProjectRole.VIEWER,
    }
    assert all(m.source == MembershipSource.IDP for m in held.values())
    assert unmapped.id not in held

    await sync_groups(session, user, ["labelers"], admin_groups=None)
    held = await _memberships(session, user.id)
    assert held[roads.id].role == ProjectRole.ANNOTATOR

    await sync_groups(session, user, [], admin_groups=None)
    assert await _memberships(session, user.id) == {}
    actions = await _actions(session)
    assert actions.count("membership.create") == 2
    assert actions.count("membership.update") == 1
    assert actions.count("membership.delete") == 2


async def test_manual_memberships_are_never_touched(session: AsyncSession) -> None:
    org = await _org(session)
    user = await _user(session, org)
    project = await _project(session, org, {"labelers": "viewer"})
    session.add(Membership(user_id=user.id, project_id=project.id, role=ProjectRole.OWNER))
    await session.flush()

    await sync_groups(session, user, ["labelers"], admin_groups=None)
    await sync_groups(session, user, [], admin_groups=None)

    (membership,) = (await _memberships(session, user.id)).values()
    assert membership.role == ProjectRole.OWNER
    assert membership.source == MembershipSource.MANUAL
    assert await _actions(session) == []


async def test_last_owner_is_kept(session: AsyncSession) -> None:
    org = await _org(session)
    user = await _user(session, org)
    project = await _project(session, org, {"leads": "owner"})

    await sync_groups(session, user, ["leads"], admin_groups=None)
    await sync_groups(session, user, [], admin_groups=None)

    (membership,) = (await _memberships(session, user.id)).values()
    assert membership.role == ProjectRole.OWNER

    other = await _user(session, org)
    session.add(Membership(user_id=other.id, project_id=project.id, role=ProjectRole.OWNER))
    await session.flush()
    await sync_groups(session, user, [], admin_groups=None)
    assert await _memberships(session, user.id) == {}


async def test_other_organisations_projects_are_ignored(session: AsyncSession) -> None:
    org, other_org = await _org(session), await _org(session)
    user = await _user(session, org)
    await _project(session, other_org, {"labelers": "owner"})

    await sync_groups(session, user, ["labelers"], admin_groups=None)

    assert await _memberships(session, user.id) == {}


# --- superuser -------------------------------------------------------------------


async def test_admin_groups_grant_and_revoke_superuser(session: AsyncSession) -> None:
    org = await _org(session)
    await _user(session, org, superuser=True)  # someone else keeps the org administrable
    user = await _user(session, org)

    await sync_groups(session, user, ["admins"], admin_groups=ADMINS)
    assert user.is_superuser is True
    await sync_groups(session, user, ["other"], admin_groups=ADMINS)
    assert user.is_superuser is False
    assert (await _actions(session)).count("user.superuser_sync") == 2


async def test_superuser_untouched_when_not_managed_or_last(session: AsyncSession) -> None:
    org = await _org(session)
    user = await _user(session, org, superuser=True)

    await sync_groups(session, user, [], admin_groups=None)
    assert user.is_superuser is True

    await sync_groups(session, user, [], admin_groups=ADMINS)
    assert user.is_superuser is True  # the last superuser is never demoted
    assert await _actions(session) == []
