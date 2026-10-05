"""Tests for `api/v1/api_keys.py` and `services/api_keys.py` (AUTH-4).

Runs the *real* `get_current_user` for API-key requests: a key is a `kid`
token whose row is resolved from the database on every call, so the tests
mint keys through the API and then use them as bearer tokens. Person calls
use a login token from `create_access_token`, exactly as `/auth/login` would.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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

from app.core.security import create_access_token, create_api_key_token
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import ApiKey, AuditEvent, Membership, Organization, Project, User
from app.services import api_keys as service


@compiles(CITEXT, "sqlite")  # pragma: no cover - exercised implicitly by every test
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, Project.__table__),
    cast(Table, Membership.__table__),
    cast(Table, ApiKey.__table__),
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
    superuser: bool = False,
) -> User:
    async with sessionmaker() as session:
        user = User(
            organization_id=organization_id,
            email=f"{uuid4().hex[:8]}@example.com",
            display_name="Person",
            is_active=True,
            is_superuser=superuser,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def _login(user: User) -> dict[str, str]:
    token = create_access_token(
        subject=str(user.id),
        extra_claims={
            "org": str(user.organization_id),
            "email": user.email,
            "superuser": user.is_superuser,
            "service": False,
        },
    )
    return {"Authorization": f"Bearer {token}"}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mint(
    client: TestClient, user: User, **body: object
) -> tuple[dict[str, object], dict[str, str]]:
    payload: dict[str, object] = {"name": "ci"}
    payload.update(body)
    response = client.post("/api/v1/api-keys", json=payload, headers=_login(user))
    assert response.status_code == 201, response.text
    data = cast(dict[str, object], response.json())
    return data, _bearer(cast(str, data["token"]))


async def _audit_actions(sessionmaker: async_sessionmaker[AsyncSession]) -> list[str]:
    async with sessionmaker() as session:
        rows = await session.scalars(select(AuditEvent.action).order_by(AuditEvent.created_at))
        return list(rows)


class TestMintAndUse:
    async def test_key_authenticates_as_its_user(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        user = await _add_user(sessionmaker, org_id)

        created, key_headers = _mint(client, user)
        assert created["scopes"] == ["read"]
        assert created["user_id"] == str(user.id)

        me = client.get("/api/v1/auth/me", headers=key_headers)
        assert me.status_code == 200
        assert me.json()["id"] == str(user.id)

        listed = client.get("/api/v1/api-keys", headers=_login(user)).json()
        assert [row["id"] for row in listed] == [created["id"]]
        assert "token" not in listed[0]
        assert listed[0]["last_used_at"] is not None
        assert await _audit_actions(sessionmaker) == ["api_key.create"]

    async def test_scopes_expand_and_reject_unknown(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        user = await _add_user(sessionmaker, org_id)

        created, _ = _mint(client, user, scopes=["admin"])
        assert created["scopes"] == ["read", "write", "admin"]

        bad = client.post(
            "/api/v1/api-keys", json={"name": "x", "scopes": ["root"]}, headers=_login(user)
        )
        assert bad.status_code == 422
        assert "root" in bad.json()["detail"]

    async def test_read_key_cannot_write(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        user = await _add_user(sessionmaker, org_id)
        _, read_headers = _mint(client, user)
        _, write_headers = _mint(client, user, scopes=["write"])

        denied = client.post("/api/v1/projects", json={"name": "P"}, headers=read_headers)
        assert denied.status_code == 403
        assert "write scope" in denied.json()["detail"]

        allowed = client.post("/api/v1/projects", json={"name": "P"}, headers=write_headers)
        assert allowed.status_code == 201, allowed.text

    async def test_key_cannot_manage_credentials(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        admin = await _add_user(sessionmaker, org_id, superuser=True)
        _, key_headers = _mint(client, admin, scopes=["admin"])

        for method, path in (
            ("post", "/api/v1/api-keys"),
            ("get", "/api/v1/api-keys"),
            ("post", "/api/v1/service-accounts"),
            ("get", "/api/v1/service-accounts"),
        ):
            response = client.request(method, path, json={"name": "x"}, headers=key_headers)
            assert response.status_code == 403, (method, path, response.text)

    async def test_admin_scope_gates_superuser_endpoints(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        admin = await _add_user(sessionmaker, org_id, superuser=True)
        _, read_headers = _mint(client, admin)
        _, admin_headers = _mint(client, admin, scopes=["admin"])

        assert client.get("/api/v1/audit", headers=read_headers).status_code == 403
        assert client.get("/api/v1/audit", headers=admin_headers).status_code == 200

    async def test_revoked_key_is_unauthorized(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        user = await _add_user(sessionmaker, org_id)
        created, key_headers = _mint(client, user)

        assert (
            client.delete(f"/api/v1/api-keys/{created['id']}", headers=_login(user)).status_code
            == 204
        )
        assert client.get("/api/v1/auth/me", headers=key_headers).status_code == 401
        # Idempotent, and a second revoke leaves no second audit row.
        assert (
            client.delete(f"/api/v1/api-keys/{created['id']}", headers=_login(user)).status_code
            == 204
        )
        assert await _audit_actions(sessionmaker) == ["api_key.create", "api_key.revoke"]

    async def test_expired_key_is_unauthorized(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        user = await _add_user(sessionmaker, org_id)
        past = datetime.now(UTC) - timedelta(minutes=1)

        rejected = client.post(
            "/api/v1/api-keys",
            json={"name": "x", "expires_at": past.isoformat()},
            headers=_login(user),
        )
        assert rejected.status_code == 422

        # A row that has since expired: the token's `exp` and the row both say no.
        async with sessionmaker() as session:
            key = ApiKey(
                organization_id=org_id,
                user_id=user.id,
                name="old",
                scopes=["read"],
                expires_at=past,
            )
            session.add(key)
            await session.commit()
            await session.refresh(key)
        token = create_api_key_token(
            key_id=str(key.id), subject=str(user.id), organization_id=str(org_id), expires_at=None
        )
        assert client.get("/api/v1/auth/me", headers=_bearer(token)).status_code == 401

    async def test_unknown_or_mismatched_kid_is_unauthorized(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        user = await _add_user(sessionmaker, org_id)
        other = await _add_user(sessionmaker, org_id)
        created, _ = _mint(client, user)

        unknown = create_api_key_token(
            key_id=str(uuid4()), subject=str(user.id), organization_id=str(org_id), expires_at=None
        )
        assert client.get("/api/v1/auth/me", headers=_bearer(unknown)).status_code == 401
        # A forged `sub` on a real `kid` does not become that other user.
        forged = create_api_key_token(
            key_id=str(created["id"]),
            subject=str(other.id),
            organization_id=str(org_id),
            expires_at=None,
        )
        assert client.get("/api/v1/auth/me", headers=_bearer(forged)).status_code == 401
        malformed = create_api_key_token(
            key_id="not-a-uuid", subject=str(user.id), organization_id=str(org_id), expires_at=None
        )
        assert client.get("/api/v1/auth/me", headers=_bearer(malformed)).status_code == 401

    async def test_last_used_is_throttled(
        self, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        user = await _add_user(sessionmaker, org_id)
        async with sessionmaker() as session:
            key, _ = await service.create_api_key(
                session,
                organization_id=org_id,
                user_id=user.id,
                name="k",
                scopes=["read"],
                expires_at=None,
                actor_id=user.id,
            )
            await session.commit()
            first, _ = await service.resolve_api_key(session, key_id=key.id, user_id=user.id)
            stamp = first.last_used_at
            assert stamp is not None
            second, _ = await service.resolve_api_key(session, key_id=key.id, user_id=user.id)
            assert second.last_used_at == stamp


class TestPermissions:
    async def test_only_superuser_issues_for_others(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        user = await _add_user(sessionmaker, org_id)
        other = await _add_user(sessionmaker, org_id)
        admin = await _add_user(sessionmaker, org_id, superuser=True)

        denied = client.post(
            "/api/v1/api-keys", json={"name": "x", "user_id": str(other.id)}, headers=_login(user)
        )
        assert denied.status_code == 403
        assert (
            client.get(f"/api/v1/api-keys?user_id={other.id}", headers=_login(user)).status_code
            == 403
        )

        created, key_headers = _mint(client, admin, user_id=str(other.id))
        assert created["user_id"] == str(other.id)
        assert created["created_by"] == str(admin.id)
        assert client.get("/api/v1/auth/me", headers=key_headers).json()["id"] == str(other.id)
        listed = client.get(f"/api/v1/api-keys?user_id={other.id}", headers=_login(admin)).json()
        assert [row["id"] for row in listed] == [created["id"]]

    async def test_cannot_issue_across_organisations(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        other_org = await _seed_org(sessionmaker)
        admin = await _add_user(sessionmaker, org_id, superuser=True)
        stranger = await _add_user(sessionmaker, other_org)

        response = client.post(
            "/api/v1/api-keys",
            json={"name": "x", "user_id": str(stranger.id)},
            headers=_login(admin),
        )
        assert response.status_code == 404

    async def test_revoke_others_key_needs_superuser(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        user = await _add_user(sessionmaker, org_id)
        other = await _add_user(sessionmaker, org_id)
        admin = await _add_user(sessionmaker, org_id, superuser=True)
        created, key_headers = _mint(client, user)

        assert (
            client.delete(f"/api/v1/api-keys/{created['id']}", headers=_login(other)).status_code
            == 403
        )
        assert (
            client.delete(f"/api/v1/api-keys/{uuid4()}", headers=_login(admin)).status_code == 404
        )
        assert (
            client.delete(f"/api/v1/api-keys/{created['id']}", headers=_login(admin)).status_code
            == 204
        )
        assert client.get("/api/v1/auth/me", headers=key_headers).status_code == 401


class TestServiceAccounts:
    async def test_lifecycle(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        admin = await _add_user(sessionmaker, org_id, superuser=True)

        created = client.post(
            "/api/v1/service-accounts", json={"display_name": "CI bot"}, headers=_login(admin)
        )
        assert created.status_code == 201, created.text
        account = created.json()
        assert account["is_service"] is True
        assert account["email"].endswith("@service.invalid")

        # It cannot sign in — there is no password and the e-mail is synthetic.
        login = client.post(
            "/api/v1/auth/login", json={"email": account["email"], "password": "anything"}
        )
        assert login.status_code == 401

        # But it acts through a key: here as a project owner.
        _, key_headers = _mint(client, admin, user_id=account["id"], scopes=["write"])
        project = client.post("/api/v1/projects", json={"name": "Bot project"}, headers=key_headers)
        assert project.status_code == 201, project.text
        assert client.get("/api/v1/auth/me", headers=key_headers).json()["id"] == account["id"]

        listed = client.get("/api/v1/service-accounts", headers=_login(admin)).json()
        assert [row["id"] for row in listed] == [account["id"]]

        # Deleting deactivates the account and kills its keys at once.
        gone = client.delete(f"/api/v1/service-accounts/{account['id']}", headers=_login(admin))
        assert gone.status_code == 204
        assert client.get("/api/v1/auth/me", headers=key_headers).status_code == 401
        listed = client.get("/api/v1/service-accounts", headers=_login(admin)).json()
        assert listed[0]["is_active"] is False
        # No new keys for a deactivated account either.
        refused = client.post(
            "/api/v1/api-keys", json={"name": "x", "user_id": account["id"]}, headers=_login(admin)
        )
        assert refused.status_code == 422

        actions = await _audit_actions(sessionmaker)
        assert actions == [
            "service_account.create",
            "api_key.create",
            "project.create",
            "service_account.delete",
        ]

    async def test_superuser_only(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        org_id = await _seed_org(sessionmaker)
        user = await _add_user(sessionmaker, org_id)
        admin = await _add_user(sessionmaker, org_id, superuser=True)

        assert (
            client.post(
                "/api/v1/service-accounts", json={"display_name": "x"}, headers=_login(user)
            ).status_code
            == 403
        )
        assert client.get("/api/v1/service-accounts", headers=_login(user)).status_code == 403
        # Only service accounts can be deleted here, never people.
        assert (
            client.delete(f"/api/v1/service-accounts/{user.id}", headers=_login(admin)).status_code
            == 404
        )


class TestOpaqueTokens:
    """`ant_…` keys (migration 0032): stored hashed, independent of APP_SECRET_KEY."""

    async def test_the_token_is_opaque_and_only_its_hash_is_stored(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        user = await _add_user(sessionmaker, await _seed_org(sessionmaker))
        created, _ = _mint(client, user)
        token = cast(str, created["token"])

        assert re.fullmatch(r"ant_[0-9a-f]{8}_[A-Za-z0-9_-]{43}", token)
        async with sessionmaker() as session:
            row = await session.get(ApiKey, UUID(str(created["id"])))
        assert row is not None
        assert row.token_prefix == token.split("_")[1]
        assert row.token_hash == service.hash_token(token)
        assert token not in (row.token_hash, row.token_prefix)

    async def test_a_wrong_secret_or_prefix_is_unauthorized(
        self, client: TestClient, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        user = await _add_user(sessionmaker, await _seed_org(sessionmaker))
        created, _ = _mint(client, user)
        token = cast(str, created["token"])
        prefix = token.split("_")[1]

        for bad in (
            f"ant_{prefix}_{'x' * 43}",  # right row, wrong secret
            token.replace(prefix, "00000000"),  # unknown row
            "ant_",
            "ant_nounderscore",
        ):
            assert client.get("/api/v1/auth/me", headers=_bearer(bad)).status_code == 401, bad

    async def test_keys_survive_a_secret_key_rotation_and_legacy_keys_need_the_previous(
        self,
        client: TestClient,
        sessionmaker: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.core.config import get_settings

        org_id = await _seed_org(sessionmaker)
        user = await _add_user(sessionmaker, org_id)
        created, opaque = _mint(client, user)
        legacy = _bearer(
            create_api_key_token(
                key_id=str(created["id"]),
                subject=str(user.id),
                organization_id=str(org_id),
                expires_at=None,
            )
        )
        old_key = get_settings().secret_key
        try:
            monkeypatch.setenv("APP_SECRET_KEY", "a-brand-new-secret-key")
            get_settings.cache_clear()
            assert client.get("/api/v1/auth/me", headers=opaque).status_code == 200
            assert client.get("/api/v1/auth/me", headers=legacy).status_code == 401

            monkeypatch.setenv("APP_SECRET_KEY_PREVIOUS", str(old_key))
            get_settings.cache_clear()
            assert client.get("/api/v1/auth/me", headers=legacy).status_code == 200
        finally:
            get_settings.cache_clear()
