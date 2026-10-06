"""Tests for `api/v1/members.py` (§4 permissions, SEC-3).

Uses a real in-memory SQLite database (unlike `test_api_items.py` /
`test_api_tasks.py`, which skip the `user` table entirely because its
`CITEXT` email column cannot be compiled by SQLite): the members endpoints
join `membership` with `user` to return `email` / `display_name`, and the
"user must exist in the caller's organization" checks query `user` directly.
A `sqlalchemy.ext.compiler.compiles` hook renders `CITEXT` as plain `VARCHAR`
on SQLite so the table can be created for this test module only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser, get_current_user
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import (
    AuditEvent,
    Membership,
    MembershipSource,
    Organization,
    Project,
    ProjectRole,
    User,
)


@compiles(CITEXT, "sqlite")  # pragma: no cover - exercised implicitly by every test
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, Project.__table__),
    cast(Table, Membership.__table__),
    cast(Table, AuditEvent.__table__),
]


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
def app(sessionmaker: async_sessionmaker[AsyncSession]) -> FastAPI:
    application = create_app()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_session] = _get_session
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _sign_in(app: FastAPI, user: CurrentUser) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


def _current_user(user_id: UUID, organization_id: UUID) -> CurrentUser:
    return CurrentUser(
        id=user_id,
        organization_id=organization_id,
        email="caller@example.com",
        is_superuser=False,
        is_service=False,
        scopes=frozenset(),
    )


async def _seed_org(sessionmaker: async_sessionmaker[AsyncSession]) -> UUID:
    async with sessionmaker() as session:
        org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
        session.add(org)
        await session.commit()
        await session.refresh(org)
        return org.id


async def _add_user(
    sessionmaker: async_sessionmaker[AsyncSession],
    organization_id: UUID,
    *,
    email: str | None = None,
    display_name: str = "Person",
) -> UUID:
    async with sessionmaker() as session:
        user = User(
            organization_id=organization_id,
            email=email or f"{uuid4().hex[:8]}@example.com",
            display_name=display_name,
            is_active=True,
            is_superuser=False,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user.id


async def _add_project(
    sessionmaker: async_sessionmaker[AsyncSession], organization_id: UUID
) -> UUID:
    async with sessionmaker() as session:
        project = Project(organization_id=organization_id, name="Project 1")
        session.add(project)
        await session.commit()
        await session.refresh(project)
        return project.id


async def _add_member(
    sessionmaker: async_sessionmaker[AsyncSession],
    project_id: UUID,
    user_id: UUID,
    role: ProjectRole,
    source: MembershipSource = MembershipSource.MANUAL,
) -> None:
    async with sessionmaker() as session:
        session.add(Membership(user_id=user_id, project_id=project_id, role=role, source=source))
        await session.commit()


async def _get_audit_events(
    sessionmaker: async_sessionmaker[AsyncSession], action: str
) -> list[AuditEvent]:
    async with sessionmaker() as session:
        rows = await session.scalars(
            select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.created_at)
        )
        return list(rows)


class TestListMembers:
    async def test_member_can_list(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        project_id = await _add_project(sessionmaker, org_id)
        owner_id = await _add_user(
            sessionmaker, org_id, email="owner@example.com", display_name="Owner"
        )
        await _add_member(sessionmaker, project_id, owner_id, ProjectRole.OWNER)
        _sign_in(app, _current_user(owner_id, org_id))

        response = client.get(f"/api/v1/projects/{project_id}/members")

        assert response.status_code == 200
        body = response.json()
        assert body == [
            {
                "user_id": str(owner_id),
                "email": "owner@example.com",
                "display_name": "Owner",
                "role": "owner",
                "source": "manual",
                "path_prefixes": None,
                "created_at": body[0]["created_at"],
            }
        ]

    async def test_non_member_is_forbidden(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        project_id = await _add_project(sessionmaker, org_id)
        outsider_id = await _add_user(sessionmaker, org_id)
        _sign_in(app, _current_user(outsider_id, org_id))

        response = client.get(f"/api/v1/projects/{project_id}/members")

        assert response.status_code == 403


class TestAddMember:
    async def test_annotator_cannot_add(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        project_id = await _add_project(sessionmaker, org_id)
        annotator_id = await _add_user(sessionmaker, org_id)
        await _add_member(sessionmaker, project_id, annotator_id, ProjectRole.ANNOTATOR)
        new_user_id = await _add_user(sessionmaker, org_id)
        _sign_in(app, _current_user(annotator_id, org_id))

        response = client.post(
            f"/api/v1/projects/{project_id}/members",
            json={"user_id": str(new_user_id), "role": "viewer"},
        )

        assert response.status_code == 403

    async def test_owner_adds_by_user_id(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        project_id = await _add_project(sessionmaker, org_id)
        owner_id = await _add_user(sessionmaker, org_id, email="owner@example.com")
        await _add_member(sessionmaker, project_id, owner_id, ProjectRole.OWNER)
        new_user_id = await _add_user(
            sessionmaker, org_id, email="new@example.com", display_name="New Person"
        )
        _sign_in(app, _current_user(owner_id, org_id))

        response = client.post(
            f"/api/v1/projects/{project_id}/members",
            json={"user_id": str(new_user_id), "role": "annotator"},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["user_id"] == str(new_user_id)
        assert body["email"] == "new@example.com"
        assert body["display_name"] == "New Person"
        assert body["role"] == "annotator"
        assert "created_at" in body

        events = await _get_audit_events(sessionmaker, "membership.create")
        assert len(events) == 1
        assert events[0].after == {
            "user_id": str(new_user_id),
            "role": "annotator",
            "path_prefixes": None,
        }
        assert events[0].before is None

    async def test_owner_adds_by_email(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        project_id = await _add_project(sessionmaker, org_id)
        owner_id = await _add_user(sessionmaker, org_id, email="owner@example.com")
        await _add_member(sessionmaker, project_id, owner_id, ProjectRole.OWNER)
        new_user_id = await _add_user(sessionmaker, org_id, email="byemail@example.com")
        _sign_in(app, _current_user(owner_id, org_id))

        response = client.post(
            f"/api/v1/projects/{project_id}/members",
            json={"email": "byemail@example.com", "role": "reviewer"},
        )

        assert response.status_code == 201
        body = response.json()
        assert body["user_id"] == str(new_user_id)
        assert body["role"] == "reviewer"

    async def test_both_identifiers_given_is_unprocessable(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        project_id = await _add_project(sessionmaker, org_id)
        owner_id = await _add_user(sessionmaker, org_id)
        await _add_member(sessionmaker, project_id, owner_id, ProjectRole.OWNER)
        _sign_in(app, _current_user(owner_id, org_id))

        response = client.post(
            f"/api/v1/projects/{project_id}/members",
            json={"user_id": str(uuid4()), "email": "x@example.com", "role": "viewer"},
        )

        assert response.status_code == 422

    async def test_neither_identifier_given_is_unprocessable(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        project_id = await _add_project(sessionmaker, org_id)
        owner_id = await _add_user(sessionmaker, org_id)
        await _add_member(sessionmaker, project_id, owner_id, ProjectRole.OWNER)
        _sign_in(app, _current_user(owner_id, org_id))

        response = client.post(
            f"/api/v1/projects/{project_id}/members",
            json={"role": "viewer"},
        )

        assert response.status_code == 422

    async def test_user_from_another_org_is_not_found(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        other_org_id = await _seed_org(sessionmaker)
        project_id = await _add_project(sessionmaker, org_id)
        owner_id = await _add_user(sessionmaker, org_id)
        await _add_member(sessionmaker, project_id, owner_id, ProjectRole.OWNER)
        outsider_id = await _add_user(sessionmaker, other_org_id, email="outsider@example.com")
        _sign_in(app, _current_user(owner_id, org_id))

        by_id = client.post(
            f"/api/v1/projects/{project_id}/members",
            json={"user_id": str(outsider_id), "role": "viewer"},
        )
        by_email = client.post(
            f"/api/v1/projects/{project_id}/members",
            json={"email": "outsider@example.com", "role": "viewer"},
        )

        assert by_id.status_code == 404
        assert by_email.status_code == 404

    async def test_duplicate_member_is_conflict(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        project_id = await _add_project(sessionmaker, org_id)
        owner_id = await _add_user(sessionmaker, org_id)
        await _add_member(sessionmaker, project_id, owner_id, ProjectRole.OWNER)
        _sign_in(app, _current_user(owner_id, org_id))

        response = client.post(
            f"/api/v1/projects/{project_id}/members",
            json={"user_id": str(owner_id), "role": "viewer"},
        )

        assert response.status_code == 409


class TestUpdateMember:
    async def test_owner_changes_role(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        project_id = await _add_project(sessionmaker, org_id)
        owner_id = await _add_user(sessionmaker, org_id)
        await _add_member(sessionmaker, project_id, owner_id, ProjectRole.OWNER)
        second_owner_id = await _add_user(sessionmaker, org_id)
        await _add_member(sessionmaker, project_id, second_owner_id, ProjectRole.OWNER)
        _sign_in(app, _current_user(owner_id, org_id))

        response = client.patch(
            f"/api/v1/projects/{project_id}/members/{second_owner_id}",
            json={"role": "reviewer"},
        )

        assert response.status_code == 200
        assert response.json()["role"] == "reviewer"

        events = await _get_audit_events(sessionmaker, "membership.update")
        assert len(events) == 1
        assert events[0].before == {
            "user_id": str(second_owner_id),
            "role": "owner",
            "path_prefixes": None,
        }
        assert events[0].after == {
            "user_id": str(second_owner_id),
            "role": "reviewer",
            "path_prefixes": None,
        }

    async def test_editing_an_idp_membership_makes_it_manual(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        """AUTH-3: an owner's choice outranks group sync from then on."""
        org_id = await _seed_org(sessionmaker)
        project_id = await _add_project(sessionmaker, org_id)
        owner_id = await _add_user(sessionmaker, org_id)
        await _add_member(sessionmaker, project_id, owner_id, ProjectRole.OWNER)
        synced_id = await _add_user(sessionmaker, org_id)
        await _add_member(
            sessionmaker, project_id, synced_id, ProjectRole.VIEWER, MembershipSource.IDP
        )
        _sign_in(app, _current_user(owner_id, org_id))

        listed = client.get(f"/api/v1/projects/{project_id}/members").json()
        assert {m["user_id"]: m["source"] for m in listed} == {
            str(owner_id): "manual",
            str(synced_id): "idp",
        }

        response = client.patch(
            f"/api/v1/projects/{project_id}/members/{synced_id}", json={"role": "annotator"}
        )

        assert response.status_code == 200
        assert response.json()["source"] == "manual"

    async def test_demoting_the_last_owner_is_conflict(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        project_id = await _add_project(sessionmaker, org_id)
        owner_id = await _add_user(sessionmaker, org_id)
        await _add_member(sessionmaker, project_id, owner_id, ProjectRole.OWNER)
        _sign_in(app, _current_user(owner_id, org_id))

        response = client.patch(
            f"/api/v1/projects/{project_id}/members/{owner_id}",
            json={"role": "annotator"},
        )

        assert response.status_code == 409

    async def test_non_owner_cannot_patch(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        project_id = await _add_project(sessionmaker, org_id)
        owner_id = await _add_user(sessionmaker, org_id)
        await _add_member(sessionmaker, project_id, owner_id, ProjectRole.OWNER)
        annotator_id = await _add_user(sessionmaker, org_id)
        await _add_member(sessionmaker, project_id, annotator_id, ProjectRole.ANNOTATOR)
        _sign_in(app, _current_user(annotator_id, org_id))

        response = client.patch(
            f"/api/v1/projects/{project_id}/members/{owner_id}",
            json={"role": "viewer"},
        )

        assert response.status_code == 403


class TestDeleteMember:
    async def test_owner_removes_a_member(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        project_id = await _add_project(sessionmaker, org_id)
        owner_id = await _add_user(sessionmaker, org_id)
        await _add_member(sessionmaker, project_id, owner_id, ProjectRole.OWNER)
        annotator_id = await _add_user(sessionmaker, org_id)
        await _add_member(sessionmaker, project_id, annotator_id, ProjectRole.ANNOTATOR)
        _sign_in(app, _current_user(owner_id, org_id))

        response = client.delete(f"/api/v1/projects/{project_id}/members/{annotator_id}")

        assert response.status_code == 204

        events = await _get_audit_events(sessionmaker, "membership.delete")
        assert len(events) == 1
        assert events[0].before == {"user_id": str(annotator_id), "role": "annotator"}

        listing = client.get(f"/api/v1/projects/{project_id}/members")
        remaining_ids = {member["user_id"] for member in listing.json()}
        assert str(annotator_id) not in remaining_ids

    async def test_removing_the_last_owner_is_conflict(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        project_id = await _add_project(sessionmaker, org_id)
        owner_id = await _add_user(sessionmaker, org_id)
        await _add_member(sessionmaker, project_id, owner_id, ProjectRole.OWNER)
        _sign_in(app, _current_user(owner_id, org_id))

        response = client.delete(f"/api/v1/projects/{project_id}/members/{owner_id}")

        assert response.status_code == 409

    async def test_a_second_owner_may_then_be_removed(
        self, app: FastAPI, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        project_id = await _add_project(sessionmaker, org_id)
        owner_id = await _add_user(sessionmaker, org_id)
        await _add_member(sessionmaker, project_id, owner_id, ProjectRole.OWNER)
        second_owner_id = await _add_user(sessionmaker, org_id)
        await _add_member(sessionmaker, project_id, second_owner_id, ProjectRole.OWNER)
        _sign_in(app, _current_user(owner_id, org_id))

        first_removal = client.delete(f"/api/v1/projects/{project_id}/members/{second_owner_id}")
        assert first_removal.status_code == 204

        second_removal = client.delete(f"/api/v1/projects/{project_id}/members/{owner_id}")
        assert second_removal.status_code == 409
