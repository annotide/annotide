"""Tests for SCIM provisioning (AUTH-3): `/scim/token` and `/scim/v2/*`.

The IdP side authenticates with the organisation's SCIM token only; group
changes drive `group_sync` through each project's `settings.idp_groups`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Coroutine, Iterator
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.api.deps import CurrentUser, get_current_user
from app.core.config import get_settings
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
    ScimGroup,
    ScimGroupMember,
    User,
)
from app.services import scim
from app.services.scim import ScimError


@compiles(CITEXT, "sqlite")  # pragma: no cover - exercised implicitly by every test
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


TOKEN = "scim_test-token"
OTHER_TOKEN = "scim_other-token"
SCIM = "/api/v1/scim/v2"
USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

_TABLES = cast(
    "list[Table]",
    [
        model.__table__
        for model in (
            Organization,
            User,
            Project,
            Membership,
            AuditEvent,
            ScimGroup,
            ScimGroupMember,
        )
    ],
)


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


@pytest.fixture
def sessionmaker() -> Iterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    async def _create() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all, tables=_TABLES)

    _run(_create())
    yield async_sessionmaker(bind=engine, expire_on_commit=False)
    _run(engine.dispose())


@pytest.fixture
def ids(sessionmaker: async_sessionmaker[AsyncSession]) -> dict[str, uuid.UUID]:
    """Two organisations with SCIM on, an admin in the first, one project mapping groups."""

    async def _seed() -> dict[str, uuid.UUID]:
        async with sessionmaker() as session:
            org = Organization(
                name="Acme", slug="acme", scim_token_hash=scim.hash_scim_token(TOKEN)
            )
            other = Organization(
                name="Other", slug="other", scim_token_hash=scim.hash_scim_token(OTHER_TOKEN)
            )
            session.add_all([org, other])
            await session.flush()
            admin = User(
                organization_id=org.id,
                email="admin@acme.test",
                display_name="Admin",
                is_active=True,
                is_superuser=True,
            )
            outsider = User(
                organization_id=other.id,
                email="someone@other.test",
                display_name="Someone",
                is_active=True,
            )
            project = Project(
                organization_id=org.id,
                name="Cars",
                workflow={},
                settings={"idp_groups": {"labellers": "annotator", "oid-leads": "reviewer"}},
            )
            session.add_all([admin, outsider, project])
            await session.commit()
            return {
                "org": org.id,
                "other": other.id,
                "admin": admin.id,
                "outsider": outsider.id,
                "project": project.id,
            }

    return _run(_seed())


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


def _auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": scim.SCIM_CONTENT_TYPE}


def _query[T](sessionmaker: async_sessionmaker[AsyncSession], statement: Any) -> list[Any]:
    async def _go() -> list[Any]:
        async with sessionmaker() as session:
            return list((await session.scalars(statement)).all())

    return _run(_go())


def _user(sessionmaker: async_sessionmaker[AsyncSession], user_id: str) -> User:
    (user,) = _query(sessionmaker, select(User).where(User.id == uuid.UUID(user_id)))
    return cast(User, user)


def _memberships(sessionmaker: async_sessionmaker[AsyncSession], user_id: str) -> list[Membership]:
    return _query(sessionmaker, select(Membership).where(Membership.user_id == uuid.UUID(user_id)))


def _actions(sessionmaker: async_sessionmaker[AsyncSession]) -> list[str]:
    return _query(sessionmaker, select(AuditEvent.action).order_by(AuditEvent.created_at))


def _create_user(client: TestClient, email: str = "ada@acme.test", **extra: Any) -> dict[str, Any]:
    body = {
        "schemas": [USER_SCHEMA],
        "userName": email,
        "name": {"givenName": "Ada", "familyName": "Lovelace"},
        "emails": [{"value": email, "type": "work", "primary": True}],
        "active": True,
        **extra,
    }
    response = client.post(f"{SCIM}/Users", json=body, headers=_auth())
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def _patch(ops: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schemas": [PATCH_SCHEMA], "Operations": ops}


# --- Token --------------------------------------------------------------------------


def _login(app: FastAPI, ids: dict[str, uuid.UUID], *, superuser: bool = True) -> None:
    user = CurrentUser(
        id=ids["admin"],
        organization_id=ids["org"],
        email="admin@acme.test",
        is_superuser=superuser,
        is_service=False,
        scopes=frozenset(),
    )
    app.dependency_overrides[get_current_user] = lambda: user


def test_token_mint_replace_and_revoke(
    app: FastAPI,
    client: TestClient,
    ids: dict[str, uuid.UUID],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    _login(app, ids)
    assert client.get("/api/v1/scim/token").json() == {"enabled": True}

    minted = client.post("/api/v1/scim/token")
    assert minted.status_code == 201
    token = minted.json()["token"]
    assert token.startswith("scim_")
    assert minted.json()["path"] == SCIM
    # The seeded token is replaced; the new one works.
    assert client.get(f"{SCIM}/Users", headers=_auth()).status_code == 401
    assert client.get(f"{SCIM}/Users", headers=_auth(token)).status_code == 200

    assert client.delete("/api/v1/scim/token").status_code == 204
    assert client.get("/api/v1/scim/token").json() == {"enabled": False}
    assert client.get(f"{SCIM}/Users", headers=_auth(token)).status_code == 401
    assert _actions(sessionmaker).count("organization.scim_token") == 2


def test_token_needs_a_superuser(
    app: FastAPI, client: TestClient, ids: dict[str, uuid.UUID]
) -> None:
    _login(app, ids, superuser=False)
    assert client.post("/api/v1/scim/token").status_code == 403


# --- Authentication and discovery ----------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer scim_wrong"}, {"Authorization": f"Basic {TOKEN}"}],
)
def test_scim_needs_the_token(
    client: TestClient, ids: dict[str, uuid.UUID], headers: dict[str, str]
) -> None:
    response = client.get(f"{SCIM}/Users", headers=headers)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith(scim.SCIM_CONTENT_TYPE)
    assert response.headers["www-authenticate"] == "Bearer"
    body = response.json()
    assert body["schemas"] == [scim.SCHEMA_ERROR]
    assert body["status"] == "401"


def test_discovery_endpoints(client: TestClient, ids: dict[str, uuid.UUID]) -> None:
    config = client.get(f"{SCIM}/ServiceProviderConfig", headers=_auth()).json()
    assert config["patch"] == {"supported": True}
    assert config["bulk"]["supported"] is False
    types = client.get(f"{SCIM}/ResourceTypes", headers=_auth()).json()
    assert [t["id"] for t in types["Resources"]] == ["User", "Group"]
    schemas = client.get(f"{SCIM}/Schemas", headers=_auth()).json()
    assert schemas["totalResults"] == 2


# --- Users ------------------------------------------------------------------------


def test_create_user_and_read_it_back(
    client: TestClient, ids: dict[str, uuid.UUID], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    created = _create_user(client, externalId="ext-1")
    assert created["userName"] == "ada@acme.test"
    assert created["displayName"] == "Ada Lovelace"
    assert created["externalId"] == "ext-1"
    assert created["active"] is True
    assert created["meta"]["location"].endswith(f"/Users/{created['id']}")

    user = _user(sessionmaker, created["id"])
    assert user.organization_id == ids["org"]
    assert user.password_hash is None
    assert user.idp_subject is None
    assert user.last_seen_at is None  # no licence seat until they sign in
    fetched = client.get(f"{SCIM}/Users/{created['id']}", headers=_auth())
    assert fetched.status_code == 200
    assert fetched.json()["id"] == created["id"]
    assert "user.provision" in _actions(sessionmaker)


def test_display_name_falls_back_to_the_email(
    client: TestClient, ids: dict[str, uuid.UUID]
) -> None:
    response = client.post(f"{SCIM}/Users", json={"userName": "grace@acme.test"}, headers=_auth())
    assert response.json()["displayName"] == "grace"


@pytest.mark.parametrize(
    ("body", "scim_type"),
    [
        ({"userName": "not-an-email"}, "invalidValue"),
        ({}, "invalidValue"),
        ({"userName": "a@acme.test", "active": "maybe"}, "invalidValue"),
        ({"userName": 7}, "invalidValue"),
    ],
)
def test_create_user_rejects_bad_bodies(
    client: TestClient, ids: dict[str, uuid.UUID], body: dict[str, Any], scim_type: str
) -> None:
    response = client.post(f"{SCIM}/Users", json=body, headers=_auth())
    assert response.status_code == 400
    assert response.json()["scimType"] == scim_type


def test_create_user_rejects_non_json_and_non_objects(
    client: TestClient, ids: dict[str, uuid.UUID]
) -> None:
    assert client.post(f"{SCIM}/Users", content=b"{", headers=_auth()).status_code == 400
    assert client.post(f"{SCIM}/Users", json=[1], headers=_auth()).status_code == 400
    huge = b'{"userName": "' + b"a" * (scim.MAX_SCIM_BODY_BYTES + 1) + b'"}'
    assert client.post(f"{SCIM}/Users", content=huge, headers=_auth()).status_code == 413


def test_create_user_conflicts_on_a_taken_email(
    client: TestClient, ids: dict[str, uuid.UUID]
) -> None:
    _create_user(client)
    again = client.post(f"{SCIM}/Users", json={"userName": "ada@acme.test"}, headers=_auth())
    assert again.status_code == 409
    assert again.json()["scimType"] == "uniqueness"
    # Another organisation's account is taken too, and not revealed beyond that.
    other = client.post(f"{SCIM}/Users", json={"userName": "someone@other.test"}, headers=_auth())
    assert other.status_code == 409


def test_list_and_filter_users(client: TestClient, ids: dict[str, uuid.UUID]) -> None:
    ada = _create_user(client, externalId="ext-ada")
    _create_user(client, email="grace@acme.test")

    everyone = client.get(f"{SCIM}/Users", headers=_auth()).json()
    # The admin (made by hand) is visible for the IdP to adopt; the other
    # organisation's user is not.
    assert everyone["totalResults"] == 3
    assert {r["userName"] for r in everyone["Resources"]} == {
        "admin@acme.test",
        "ada@acme.test",
        "grace@acme.test",
    }

    for query in (
        'userName eq "ada@acme.test"',
        'externalId eq "ext-ada"',
        f'id eq "{ada["id"]}"',
        'emails.value eq "ada@acme.test"',
        'urn:ietf:params:scim:schemas:core:2.0:User:userName EQ "ada@acme.test"',
    ):
        found = client.get(f"{SCIM}/Users", params={"filter": query}, headers=_auth()).json()
        assert [r["id"] for r in found["Resources"]] == [ada["id"]], query

    none = client.get(f"{SCIM}/Users", params={"filter": 'id eq "nope"'}, headers=_auth())
    assert none.json()["totalResults"] == 0

    page = client.get(f"{SCIM}/Users", params={"startIndex": 2, "count": 1}, headers=_auth()).json()
    assert page["startIndex"] == 2
    assert page["itemsPerPage"] == 1
    assert page["totalResults"] == 3
    count_only = client.get(f"{SCIM}/Users", params={"count": 0}, headers=_auth()).json()
    assert count_only["Resources"] == []
    assert count_only["totalResults"] == 3


def test_malformed_paging_is_a_scim_400(client: TestClient, ids: dict[str, uuid.UUID]) -> None:
    response = client.get(f"{SCIM}/Users", params={"startIndex": "x"}, headers=_auth())
    assert response.status_code == 400
    assert response.json()["scimType"] == "invalidValue"


def test_unsupported_filters_are_400(client: TestClient, ids: dict[str, uuid.UUID]) -> None:
    for query in ('displayName eq "x"', 'userName co "a"', "userName eq x"):
        response = client.get(f"{SCIM}/Users", params={"filter": query}, headers=_auth())
        assert response.status_code == 400, query
        assert response.json()["scimType"] == "invalidFilter"


def test_users_of_another_organisation_are_404(
    client: TestClient, ids: dict[str, uuid.UUID]
) -> None:
    ada = _create_user(client)
    assert client.get(f"{SCIM}/Users/{ada['id']}", headers=_auth(OTHER_TOKEN)).status_code == 404
    assert client.get(f"{SCIM}/Users/not-a-uuid", headers=_auth()).status_code == 404
    outsider = client.get(f"{SCIM}/Users/{ids['outsider']}", headers=_auth())
    assert outsider.status_code == 404
    assert outsider.json()["status"] == "404"


def test_replace_user(
    client: TestClient, ids: dict[str, uuid.UUID], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ada = _create_user(client, externalId="ext-1")
    replaced = client.put(
        f"{SCIM}/Users/{ada['id']}",
        json={"userName": "ada.l@acme.test", "displayName": "Ada L", "active": False},
        headers=_auth(),
    )
    assert replaced.status_code == 200, replaced.text
    body = replaced.json()
    assert body["userName"] == "ada.l@acme.test"
    assert body["displayName"] == "Ada L"
    assert body["active"] is False
    assert "externalId" not in body  # a PUT without it clears it
    actions = _actions(sessionmaker)
    assert "user.update" in actions
    assert "user.deactivate" in actions

    taken = client.put(
        f"{SCIM}/Users/{ada['id']}", json={"userName": "admin@acme.test"}, headers=_auth()
    )
    assert taken.status_code == 409


def test_patch_user_entra_style(
    client: TestClient, ids: dict[str, uuid.UUID], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ada = _create_user(client)
    response = client.patch(
        f"{SCIM}/Users/{ada['id']}",
        json=_patch(
            [
                {"op": "Replace", "path": "active", "value": "False"},
                {"op": "Replace", "path": "displayName", "value": "Countess"},
                {"op": "Add", "path": "externalId", "value": "oid-123"},
                {"op": "Replace", "path": 'emails[type eq "work"].value', "value": "x@y.z"},
                {
                    "op": "Add",
                    "path": "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User:department",
                    "value": "R&D",
                },
            ]
        ),
        headers=_auth(),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["active"] is False
    assert body["displayName"] == "Countess"
    assert body["externalId"] == "oid-123"
    assert body["userName"] == "ada@acme.test"  # emails are not read

    # Path-less form with dotted keys, then removing externalId.
    response = client.patch(
        f"{SCIM}/Users/{ada['id']}",
        json=_patch(
            [
                {"op": "replace", "value": {"active": True, "name.formatted": "Ada"}},
                {"op": "remove", "path": "externalId"},
            ]
        ),
        headers=_auth(),
    )
    body = response.json()
    assert body["active"] is True
    assert body["displayName"] == "Ada"
    assert "externalId" not in body
    assert _user(sessionmaker, ada["id"]).is_active is True


@pytest.mark.parametrize(
    "body",
    [
        {"Operations": []},
        {"Operations": ["x"]},
        {"Operations": [{"op": "move", "path": "active", "value": True}]},
        {"Operations": [{"op": "remove"}]},
        {"Operations": [{"op": "replace", "path": 3, "value": True}]},
        {"Operations": [{"op": "replace", "value": "nope"}]},
    ],
)
def test_patch_user_rejects_malformed_operations(
    client: TestClient, ids: dict[str, uuid.UUID], body: dict[str, Any]
) -> None:
    ada = _create_user(client)
    response = client.patch(f"{SCIM}/Users/{ada['id']}", json=body, headers=_auth())
    assert response.status_code == 400


def test_last_admin_is_never_deactivated(client: TestClient, ids: dict[str, uuid.UUID]) -> None:
    response = client.patch(
        f"{SCIM}/Users/{ids['admin']}",
        json=_patch([{"op": "replace", "path": "active", "value": False}]),
        headers=_auth(),
    )
    assert response.status_code == 409
    assert response.json()["scimType"] == "mutability"
    assert client.delete(f"{SCIM}/Users/{ids['admin']}", headers=_auth()).status_code == 409


def test_delete_hides_and_deactivates_then_post_revives(
    client: TestClient, ids: dict[str, uuid.UUID], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ada = _create_user(client)
    assert client.delete(f"{SCIM}/Users/{ada['id']}", headers=_auth()).status_code == 204
    assert client.get(f"{SCIM}/Users/{ada['id']}", headers=_auth()).status_code == 404
    listed = client.get(
        f"{SCIM}/Users", params={"filter": 'userName eq "ada@acme.test"'}, headers=_auth()
    ).json()
    assert listed["totalResults"] == 0
    user = _user(sessionmaker, ada["id"])
    assert user.is_active is False
    assert user.scim_deleted_at is not None
    assert user.erased_at is None  # not an erasure

    revived = _create_user(client)
    assert revived["id"] == ada["id"]
    assert revived["active"] is True
    assert _user(sessionmaker, ada["id"]).scim_deleted_at is None


# --- Groups -----------------------------------------------------------------------


def _create_group(
    client: TestClient, name: str, members: list[str], **extra: Any
) -> dict[str, Any]:
    response = client.post(
        f"{SCIM}/Groups",
        json={"displayName": name, "members": [{"value": m} for m in members], **extra},
        headers=_auth(),
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def _roles(sessionmaker: async_sessionmaker[AsyncSession], user_id: str) -> list[tuple[str, str]]:
    return [(m.role.value, m.source.value) for m in _memberships(sessionmaker, user_id)]


def test_group_membership_drives_project_roles(
    client: TestClient, ids: dict[str, uuid.UUID], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ada = _create_user(client)
    group = _create_group(client, "labellers", [ada["id"]])
    assert [m["value"] for m in group["members"]] == [ada["id"]]
    assert _roles(sessionmaker, ada["id"]) == [("annotator", "idp")]

    # The group's external id (an Entra object id) maps too: highest role wins.
    leads = _create_group(client, "Team leads", [], externalId="oid-leads")
    response = client.patch(
        f"{SCIM}/Groups/{leads['id']}",
        json=_patch([{"op": "add", "path": "members", "value": [{"value": ada["id"]}]}]),
        headers=_auth(),
    )
    assert response.status_code == 204
    assert _roles(sessionmaker, ada["id"]) == [("reviewer", "idp")]

    # Removing her from both groups removes the membership sync made.
    client.patch(
        f"{SCIM}/Groups/{leads['id']}",
        json=_patch([{"op": "remove", "path": f'members[value eq "{ada["id"]}"]'}]),
        headers=_auth(),
    )
    assert _roles(sessionmaker, ada["id"]) == [("annotator", "idp")]
    client.patch(
        f"{SCIM}/Groups/{group['id']}",
        json=_patch([{"op": "remove", "path": "members", "value": [{"value": ada["id"]}]}]),
        headers=_auth(),
    )
    assert _roles(sessionmaker, ada["id"]) == []

    (event,) = _query(
        sessionmaker,
        select(AuditEvent).where(AuditEvent.action == "membership.create").limit(1),
    )
    assert event.actor_id is None
    assert event.after["via"] == "scim"


def test_renaming_a_group_resyncs_its_members(
    client: TestClient, ids: dict[str, uuid.UUID], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ada = _create_user(client)
    group = _create_group(client, "unmapped", [ada["id"]])
    assert _roles(sessionmaker, ada["id"]) == []
    client.patch(
        f"{SCIM}/Groups/{group['id']}",
        json=_patch([{"op": "replace", "value": {"displayName": "labellers"}}]),
        headers=_auth(),
    )
    assert _roles(sessionmaker, ada["id"]) == [("annotator", "idp")]


def test_manual_memberships_are_left_alone(
    client: TestClient, ids: dict[str, uuid.UUID], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ada = _create_user(client)

    async def _manual() -> None:
        async with sessionmaker() as session:
            session.add(
                Membership(
                    user_id=uuid.UUID(ada["id"]),
                    project_id=ids["project"],
                    role=ProjectRole.OWNER,
                    source=MembershipSource.MANUAL,
                )
            )
            await session.commit()

    _run(_manual())
    group = _create_group(client, "labellers", [ada["id"]])
    assert client.delete(f"{SCIM}/Groups/{group['id']}", headers=_auth()).status_code == 204
    assert _roles(sessionmaker, ada["id"]) == [("owner", "manual")]


def test_replace_and_delete_group(
    client: TestClient, ids: dict[str, uuid.UUID], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ada = _create_user(client)
    grace = _create_user(client, email="grace@acme.test")
    group = _create_group(client, "labellers", [ada["id"]])

    replaced = client.put(
        f"{SCIM}/Groups/{group['id']}",
        json={"displayName": "labellers", "members": [{"value": grace["id"]}]},
        headers=_auth(),
    )
    assert replaced.status_code == 200
    assert [m["value"] for m in replaced.json()["members"]] == [grace["id"]]
    assert _roles(sessionmaker, ada["id"]) == []
    assert _roles(sessionmaker, grace["id"]) == [("annotator", "idp")]

    assert client.delete(f"{SCIM}/Groups/{group['id']}", headers=_auth()).status_code == 204
    assert _roles(sessionmaker, grace["id"]) == []
    assert client.get(f"{SCIM}/Groups/{group['id']}", headers=_auth()).status_code == 404
    assert "scim_group.delete" in _actions(sessionmaker)


def test_list_and_filter_groups(client: TestClient, ids: dict[str, uuid.UUID]) -> None:
    ada = _create_user(client)
    group = _create_group(client, "labellers", [ada["id"]], externalId="oid-1")
    _create_group(client, "others", [])

    listed = client.get(
        f"{SCIM}/Groups", params={"excludedAttributes": "members"}, headers=_auth()
    ).json()
    assert listed["totalResults"] == 2
    assert all("members" not in r for r in listed["Resources"])
    for query in ('displayName eq "labellers"', 'externalId eq "oid-1"', f'id eq "{group["id"]}"'):
        found = client.get(f"{SCIM}/Groups", params={"filter": query}, headers=_auth()).json()
        assert [r["id"] for r in found["Resources"]] == [group["id"]], query
    assert (
        client.get(f"{SCIM}/Groups", params={"filter": 'id eq "x"'}, headers=_auth()).json()[
            "totalResults"
        ]
        == 0
    )
    read = client.get(f"{SCIM}/Groups/{group['id']}", headers=_auth()).json()
    assert read["externalId"] == "oid-1"
    assert [m["value"] for m in read["members"]] == [ada["id"]]
    # The user's read-only groups attribute lists it.
    user = client.get(f"{SCIM}/Users/{ada['id']}", headers=_auth()).json()
    assert [g["display"] for g in user["groups"]] == ["labellers"]
    # Another organisation's token sees none of it.
    assert client.get(f"{SCIM}/Groups/{group['id']}", headers=_auth(OTHER_TOKEN)).status_code == 404


def test_group_validation(client: TestClient, ids: dict[str, uuid.UUID]) -> None:
    _create_group(client, "labellers", [])
    duplicate = client.post(f"{SCIM}/Groups", json={"displayName": "labellers"}, headers=_auth())
    assert duplicate.status_code == 409
    unknown = client.post(
        f"{SCIM}/Groups",
        json={"displayName": "x", "members": [{"value": str(ids["outsider"])}]},
        headers=_auth(),
    )
    assert unknown.status_code == 400
    assert unknown.json()["scimType"] == "invalidValue"
    malformed = client.post(
        f"{SCIM}/Groups", json={"displayName": "y", "members": ["nope"]}, headers=_auth()
    )
    assert malformed.status_code == 400
    assert client.post(f"{SCIM}/Groups", json={}, headers=_auth()).status_code == 400


def test_group_patch_replace_and_remove_all(
    client: TestClient, ids: dict[str, uuid.UUID], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ada = _create_user(client)
    grace = _create_user(client, email="grace@acme.test")
    group = _create_group(client, "labellers", [ada["id"]])
    client.patch(
        f"{SCIM}/Groups/{group['id']}",
        json=_patch(
            [
                {"op": "replace", "path": "members", "value": [{"value": grace["id"]}]},
                {"op": "replace", "path": "externalId", "value": "oid-9"},
            ]
        ),
        headers=_auth(),
    )
    read = client.get(f"{SCIM}/Groups/{group['id']}", headers=_auth()).json()
    assert [m["value"] for m in read["members"]] == [grace["id"]]
    assert read["externalId"] == "oid-9"
    client.patch(
        f"{SCIM}/Groups/{group['id']}",
        json=_patch([{"op": "remove", "path": "members"}, {"op": "remove", "path": "externalId"}]),
        headers=_auth(),
    )
    read = client.get(f"{SCIM}/Groups/{group['id']}", headers=_auth()).json()
    assert read["members"] == []
    assert "externalId" not in read
    assert _roles(sessionmaker, grace["id"]) == []


def test_deleting_a_user_drops_their_group_roles(
    client: TestClient, ids: dict[str, uuid.UUID], sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    ada = _create_user(client)
    group = _create_group(client, "labellers", [ada["id"]])
    assert client.delete(f"{SCIM}/Users/{ada['id']}", headers=_auth()).status_code == 204
    assert _roles(sessionmaker, ada["id"]) == []
    read = client.get(f"{SCIM}/Groups/{group['id']}", headers=_auth()).json()
    assert read["members"] == []


def test_admin_groups_apply_to_scim_groups(
    app: FastAPI,
    client: TestClient,
    ids: dict[str, uuid.UUID],
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    configured = get_settings().model_copy(update={"oidc_admin_groups": "platform-admins"})
    app.dependency_overrides[get_settings] = lambda: configured
    ada = _create_user(client)
    group = _create_group(client, "platform-admins", [ada["id"]])
    assert _user(sessionmaker, ada["id"]).is_superuser is True
    client.delete(f"{SCIM}/Groups/{group['id']}", headers=_auth())
    assert _user(sessionmaker, ada["id"]).is_superuser is False


# --- Service helpers ----------------------------------------------------------------


def test_page_window_clamps() -> None:
    assert scim.page_window(None, None) == (1, scim.DEFAULT_COUNT)
    assert scim.page_window(0, 500) == (1, scim.MAX_COUNT)
    assert scim.page_window(5, -1) == (5, 0)


def test_parse_filter_unescapes_quotes() -> None:
    parsed = scim.parse_filter(r'userName eq "a\"b@x.y"', scim.USER_FILTER_ATTRS)
    assert parsed == scim.Filter("username", 'a"b@x.y')
    assert scim.parse_filter("  ", scim.USER_FILTER_ATTRS) is None
    with pytest.raises(ScimError):
        scim.parse_filter('userName eq "a" and active eq "true"', scim.USER_FILTER_ATTRS)
