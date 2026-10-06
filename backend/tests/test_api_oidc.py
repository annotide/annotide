"""Tests for OIDC single sign-on (AUTH-1): `services/oidc.py` and the
`/auth/providers`, `/auth/oidc/login` and `/auth/oidc/callback` routes.

The identity provider is a fake served through `httpx.MockTransport`: it
answers discovery, the JWKS and the token endpoint, and signs ID tokens with
an RSA key generated per module. Like `test_api_members.py`, the module uses
an in-memory SQLite database with `CITEXT` compiled as `VARCHAR`.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm
from sqlalchemy import Table, select, update
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.api.v1 import auth as auth_mod
from app.core.config import Settings, get_settings
from app.core.security import decode_token
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import (
    AuditEvent,
    LicenseState,
    Membership,
    MembershipSource,
    Organization,
    Project,
    ProjectRole,
    ScimGroup,
    User,
)
from app.services.licensing import license as license_mod
from app.services.licensing.issue import generate_keypair
from app.services.oidc import (
    FlowState,
    Identity,
    OidcClient,
    OidcError,
    decode_flow,
    encode_flow,
    get_oidc_client,
    identity_from_claims,
    new_flow,
    safe_next_path,
)

ISSUER = "https://idp.example.test/realm"
CLIENT_ID = "annotide"
REDIRECT_URI = "http://api.test/api/v1/auth/oidc/callback"
FRONTEND_URL = "http://app.test"
KID = "test-key-1"


@compiles(CITEXT, "sqlite")  # pragma: no cover - exercised implicitly by every test
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


_TABLES: list[Table] = [
    cast(Table, Organization.__table__),
    cast(Table, User.__table__),
    cast(Table, AuditEvent.__table__),
    cast(Table, LicenseState.__table__),
    cast(Table, Project.__table__),
    cast(Table, Membership.__table__),
    cast(Table, ScimGroup.__table__),
]


# --- Fake identity provider ---------------------------------------------------


class FakeIdp:
    """Enough of an OpenID provider for the code flow, behind a MockTransport."""

    def __init__(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        public = cast(dict[str, Any], RSAAlgorithm.to_jwk(key.public_key(), as_dict=True))
        self.jwks: dict[str, Any] = {
            "keys": [{**public, "kid": KID, "use": "sig"}],
        }
        #: Claims the next token exchange puts in the ID token. Tests set it.
        self.claims: dict[str, Any] = {}
        #: Overrides for the next ID token's `nonce`, `aud`, `iss`, `kid` (None:
        #: no `kid` header) and `hs256_secret` (sign HS256 instead of RS256).
        self.overrides: dict[str, Any] = {}
        self.token_requests: list[dict[str, str]] = []
        self.token_status = 200
        self.discovery_status = 200
        self.jwks_fetches = 0
        #: Whether discovery advertises `end_session_endpoint`.
        self.end_session = True

    def id_token(self, nonce: str) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "subject-1",
            "exp": now + 300,
            "iat": now,
            "nonce": nonce,
            **self.claims,
        }
        for key in ("nonce", "aud", "iss"):
            if key in self.overrides:
                claims[key] = self.overrides[key]
        kid = self.overrides.get("kid", KID)
        headers = {"kid": kid} if kid is not None else None
        if "hs256_secret" in self.overrides:
            # A forger: HMAC with a secret of its choosing instead of the key.
            return jwt.encode(
                claims, self.overrides["hs256_secret"], algorithm="HS256", headers=headers
            )
        return jwt.encode(claims, self.private_pem, algorithm="RS256", headers=headers)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            if self.discovery_status != 200:
                return httpx.Response(self.discovery_status, text="down")
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
                    "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
                    "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
                    **(
                        {"end_session_endpoint": f"{ISSUER}/protocol/openid-connect/logout"}
                        if self.end_session
                        else {}
                    ),
                },
            )
        if path.endswith("/certs"):
            self.jwks_fetches += 1
            return httpx.Response(200, json=self.jwks)
        if path.endswith("/token"):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append(form)
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "invalid_grant"})
            # The nonce the client sent in the authorization request is not
            # visible on the token endpoint; the flow cookie test passes it
            # through the "code" for the fake.
            nonce = form["code"].removeprefix("code-for-")
            return httpx.Response(
                200,
                json={
                    "access_token": "provider-access-token",
                    "token_type": "Bearer",
                    "id_token": self.id_token(nonce),
                },
            )
        return httpx.Response(404, text=f"unexpected {path}")


# --- Fixtures ------------------------------------------------------------------


@pytest.fixture
def oidc_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    monkeypatch.setenv("APP_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("APP_OIDC_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("APP_OIDC_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("APP_OIDC_REDIRECT_URI", REDIRECT_URI)
    monkeypatch.setenv("APP_FRONTEND_URL", FRONTEND_URL)
    monkeypatch.setenv("APP_OIDC_DISPLAY_NAME", "Acme SSO")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture
def oidc_client(oidc_env: Settings, idp: FakeIdp) -> OidcClient:
    transport = httpx.MockTransport(idp.handle)
    return OidcClient(oidc_env, http=httpx.AsyncClient(transport=transport))


@pytest.fixture
async def sessionmaker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_TABLES)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
def app(
    oidc_env: Settings, oidc_client: OidcClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> FastAPI:
    application = create_app()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_session] = _get_session
    application.dependency_overrides[get_oidc_client] = lambda: oidc_client
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False)


async def _seed_org(
    sessionmaker: async_sessionmaker[AsyncSession], slug: str = "acme"
) -> Organization:
    async with sessionmaker() as session:
        org = Organization(name=slug.title(), slug=slug)
        session.add(org)
        await session.commit()
        await session.refresh(org)
        return org


async def _add_user(
    sessionmaker: async_sessionmaker[AsyncSession],
    organization: Organization,
    *,
    email: str,
    idp_subject: str | None = None,
    is_active: bool = True,
) -> User:
    async with sessionmaker() as session:
        user = User(
            organization_id=organization.id,
            email=email,
            display_name="Existing Person",
            idp_subject=idp_subject,
            password_hash="argon2-placeholder",
            is_active=is_active,
            is_superuser=False,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _users(sessionmaker: async_sessionmaker[AsyncSession]) -> list[User]:
    async with sessionmaker() as session:
        return list((await session.scalars(select(User))).all())


async def _audit_actions(sessionmaker: async_sessionmaker[AsyncSession]) -> list[str]:
    async with sessionmaker() as session:
        rows = await session.scalars(select(AuditEvent).order_by(AuditEvent.created_at))
        return [row.action for row in rows]


def _start_login(client: TestClient, next_path: str | None = None) -> str:
    """Hit the login route; return the `state` from the provider redirect."""
    params = {"next": next_path} if next_path else {}
    response = client.get("/api/v1/auth/oidc/login", params=params)
    assert response.status_code == 303, response.text
    query = parse_qs(urlparse(response.headers["location"]).query)
    return query["state"][0]


def _flow_nonce(client: TestClient, settings: Settings) -> str:
    """The nonce sealed in the flow cookie the client currently holds."""
    cookie = client.cookies.get("oidc_flow")
    assert cookie is not None
    return decode_flow(cookie, settings).nonce


def _callback(client: TestClient, settings: Settings, state: str, **extra: str) -> httpx.Response:
    nonce = _flow_nonce(client, settings)
    params = {"code": f"code-for-{nonce}", "state": state, **extra}
    return client.get("/api/v1/auth/oidc/callback", params=params)


def _fragment(response: httpx.Response) -> dict[str, str]:
    location = urlparse(response.headers["location"])
    return {k: v[0] for k, v in parse_qs(location.fragment).items()}


# --- /auth/providers -----------------------------------------------------------


def test_providers_lists_sso_when_configured(client: TestClient) -> None:
    body = client.get("/api/v1/auth/providers").json()
    assert body == {
        "local": True,
        "oidc": {"display_name": "Acme SSO", "login_path": "/auth/oidc/login"},
    }


def test_providers_without_oidc(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    get_settings.cache_clear()
    application = create_app()
    with TestClient(application) as plain:
        body = plain.get("/api/v1/auth/providers").json()
    assert body == {"local": True, "oidc": None}


def test_oidc_routes_are_404_when_not_configured() -> None:
    get_settings.cache_clear()
    with TestClient(create_app(), follow_redirects=False) as plain:
        assert plain.get("/api/v1/auth/oidc/login").status_code == 404
        assert plain.get("/api/v1/auth/oidc/callback").status_code == 404
        assert plain.get("/api/v1/auth/oidc/logout").status_code == 404


# --- /auth/oidc/login ----------------------------------------------------------


def test_login_redirects_to_provider_with_pkce(client: TestClient, oidc_env: Settings) -> None:
    response = client.get("/api/v1/auth/oidc/login", params={"next": "/projects/p1"})
    assert response.status_code == 303
    location = urlparse(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == (
        f"{ISSUER}/protocol/openid-connect/auth"
    )
    query = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert query["response_type"] == "code"
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == REDIRECT_URI
    assert query["scope"] == "openid profile email"
    assert query["code_challenge_method"] == "S256"
    assert len(query["state"]) > 20
    assert len(query["nonce"]) > 20

    cookie = response.headers["set-cookie"]
    assert "oidc_flow=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/api/v1/auth/oidc" in cookie
    flow = decode_flow(client.cookies["oidc_flow"], oidc_env)
    assert flow.state == query["state"]
    # Development over plain HTTP: the cookie must still work without TLS.
    assert "Secure" not in cookie
    assert flow.nonce == query["nonce"]
    assert flow.next_path == "/projects/p1"


def test_login_cookie_is_secure_behind_trusted_tls_proxy(
    monkeypatch: pytest.MonkeyPatch,
    oidc_client: OidcClient,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """`X-Forwarded-Proto: https` from a listed proxy sets `Secure` even in development."""
    monkeypatch.setenv("APP_TRUSTED_PROXIES", "10.0.0.1")
    get_settings.cache_clear()
    application = create_app()
    application.dependency_overrides[get_oidc_client] = lambda: oidc_client
    with TestClient(application, follow_redirects=False, client=("10.0.0.1", 1)) as proxied:
        response = proxied.get("/api/v1/auth/oidc/login", headers={"X-Forwarded-Proto": "https"})
    assert response.status_code == 303
    assert "Secure" in response.headers["set-cookie"]

    # The same header from an unlisted peer changes nothing.
    with TestClient(application, follow_redirects=False, client=("192.0.2.1", 1)) as direct:
        response = direct.get("/api/v1/auth/oidc/login", headers={"X-Forwarded-Proto": "https"})
    assert "Secure" not in response.headers["set-cookie"]
    get_settings.cache_clear()


def test_login_when_provider_is_down(client: TestClient, idp: FakeIdp) -> None:
    idp.discovery_status = 503
    response = client.get("/api/v1/auth/oidc/login")
    assert response.status_code == 303
    assert response.headers["location"] == f"{FRONTEND_URL}/login?error=provider_unreachable"


# --- /auth/oidc/callback -------------------------------------------------------


async def test_callback_provisions_a_new_user(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    org = await _seed_org(sessionmaker)
    idp.claims = {"sub": "entra-abc", "email": "New.Person@Example.com", "name": "New Person"}

    state = _start_login(client, "/projects/p1")
    response = _callback(client, oidc_env, state)

    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith(f"{FRONTEND_URL}/auth/callback#")
    fragment = _fragment(response)
    assert fragment["next"] == "/projects/p1"
    assert fragment["expires_in"] == str(oidc_env.access_token_ttl)
    # The cookie is gone once the flow completes.
    assert "oidc_flow=" in response.headers["set-cookie"]
    assert 'oidc_flow=""' in response.headers["set-cookie"]

    users = await _users(sessionmaker)
    assert len(users) == 1
    user = users[0]
    assert user.email == "new.person@example.com"
    assert user.display_name == "New Person"
    assert user.idp_subject == "entra-abc"
    assert user.organization_id == org.id
    assert user.password_hash is None
    assert user.last_seen_at is not None

    claims = decode_token(fragment["access_token"])
    assert claims["sub"] == str(user.id)
    assert claims["org"] == str(org.id)
    assert claims["email"] == user.email

    assert await _audit_actions(sessionmaker) == ["user.provision", "auth.login"]

    # The token exchange used PKCE and the confidential-client secret.
    (form,) = idp.token_requests
    assert form["grant_type"] == "authorization_code"
    assert form["redirect_uri"] == REDIRECT_URI
    assert form["client_secret"] == "client-secret"
    assert len(form["code_verifier"]) >= 43


async def test_callback_links_existing_user_by_email(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    org = await _seed_org(sessionmaker)
    existing = await _add_user(sessionmaker, org, email="person@example.com")
    idp.claims = {
        "sub": "sub-42",
        "email": "person@example.com",
        "name": "Person",
        "email_verified": True,
    }

    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert response.status_code == 303
    assert decode_token(_fragment(response)["access_token"])["sub"] == str(existing.id)

    (user,) = await _users(sessionmaker)
    assert user.id == existing.id
    assert user.idp_subject == "sub-42"
    assert user.display_name == "Existing Person"  # the IdP does not rename people
    assert await _audit_actions(sessionmaker) == ["user.link_idp", "auth.login"]


@pytest.mark.parametrize("verified", [None, False, "false"])
async def test_callback_will_not_link_an_unverified_email(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
    verified: object,
) -> None:
    # A provider that lets anyone register admin@example.com must not hand
    # over the local admin account.
    org = await _seed_org(sessionmaker)
    await _add_user(sessionmaker, org, email="admin@example.com")
    idp.claims = {"sub": "attacker", "email": "admin@example.com"}
    if verified is not None:
        idp.claims["email_verified"] = verified

    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert response.headers["location"] == f"{FRONTEND_URL}/login?error=email_unverified"
    (user,) = await _users(sessionmaker)
    assert user.idp_subject is None
    assert await _audit_actions(sessionmaker) == []


async def test_callback_links_a_scim_account_without_email_verified(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Entra SCIM pushes the account, Entra SSO signs in without email_verified.
    org = await _seed_org(sessionmaker)
    existing = await _add_user(sessionmaker, org, email="ada@example.com")
    async with sessionmaker() as session:
        await session.execute(
            update(User).where(User.id == existing.id).values(scim_external_id="ada")
        )
        await session.commit()
    idp.claims = {"sub": "entra-ada", "preferred_username": "ada@example.com"}

    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert decode_token(_fragment(response)["access_token"])["sub"] == str(existing.id)


async def test_callback_links_an_unverified_email_when_trusted(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(oidc_env, "oidc_trust_unverified_email", True)
    org = await _seed_org(sessionmaker)
    existing = await _add_user(sessionmaker, org, email="ada@example.com")
    # Entra ID: the address arrives as preferred_username, with no email_verified.
    idp.claims = {"sub": "entra-ada", "preferred_username": "ada@example.com"}

    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert decode_token(_fragment(response)["access_token"])["sub"] == str(existing.id)


async def test_callback_matches_by_subject_before_email(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    org = await _seed_org(sessionmaker)
    linked = await _add_user(sessionmaker, org, email="old@example.com", idp_subject="sub-1")
    await _add_user(sessionmaker, org, email="new@example.com")
    # Same subject, e-mail changed at the IdP: the subject wins.
    idp.claims = {"sub": "sub-1", "email": "new@example.com"}

    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert decode_token(_fragment(response)["access_token"])["sub"] == str(linked.id)
    assert await _audit_actions(sessionmaker) == ["auth.login"]


async def test_callback_refuses_email_linked_to_another_subject(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    org = await _seed_org(sessionmaker)
    await _add_user(sessionmaker, org, email="person@example.com", idp_subject="sub-original")
    idp.claims = {"sub": "sub-other", "email": "person@example.com"}

    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert response.headers["location"] == f"{FRONTEND_URL}/login?error=account_conflict"
    assert await _audit_actions(sessionmaker) == []


async def test_callback_inactive_user_is_refused_and_audited(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    org = await _seed_org(sessionmaker)
    await _add_user(sessionmaker, org, email="gone@example.com", is_active=False)
    idp.claims = {"sub": "sub-gone", "email": "gone@example.com", "email_verified": True}

    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert response.headers["location"] == f"{FRONTEND_URL}/login?error=inactive"
    assert await _audit_actions(sessionmaker) == ["user.link_idp", "auth.login_failed"]


async def test_callback_refuses_a_fourth_person_on_community(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A build with a vendor key enforces Community's three-person limit (LIC-23).
    # SSO itself is a Business feature unlicensed in Community (LIC-33); stub
    # it licensed so this test isolates the seat check, not the feature gate.
    _, public_key = generate_keypair()
    monkeypatch.setitem(license_mod.VENDOR_PUBLIC_KEYS, "voidc", public_key)
    monkeypatch.setattr(auth_mod, "has_feature", lambda _licence, _feature: True)
    org = await _seed_org(sessionmaker)
    active = [
        await _add_user(sessionmaker, org, email=f"{name}@example.com")
        for name in ("anna", "ben", "cara")
    ]
    async with sessionmaker() as session:
        await session.execute(
            update(User)
            .where(User.id.in_([user.id for user in active]))
            .values(last_seen_at=datetime.now(UTC))
        )
        await session.commit()
    await _add_user(sessionmaker, org, email="dan@example.com")
    idp.claims = {"sub": "sub-dan", "email": "dan@example.com", "email_verified": True}

    state = _start_login(client)
    response = _callback(client, oidc_env, state)

    assert response.headers["location"] == f"{FRONTEND_URL}/login?error=seat_limit"
    assert await _audit_actions(sessionmaker) == ["user.link_idp", "auth.login_failed"]


async def test_callback_without_provisioning_refuses_unknown_user(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_org(sessionmaker)
    monkeypatch.setattr(oidc_env, "oidc_auto_provision", False)
    idp.claims = {"sub": "sub-new", "email": "new@example.com"}

    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert response.headers["location"] == f"{FRONTEND_URL}/login?error=not_provisioned"
    assert await _users(sessionmaker) == []


async def test_callback_needs_an_organization_to_provision_into(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    idp.claims = {"sub": "sub-new", "email": "new@example.com"}

    # No organisation at all.
    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert response.headers["location"] == f"{FRONTEND_URL}/login?error=no_organization"

    # Two organisations and no slug configured: ambiguous.
    await _seed_org(sessionmaker, "one")
    two = await _seed_org(sessionmaker, "two")
    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert response.headers["location"] == f"{FRONTEND_URL}/login?error=no_organization"

    # A configured slug picks one.
    monkeypatch.setattr(oidc_env, "oidc_organization_slug", "two")
    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert response.headers["location"].startswith(f"{FRONTEND_URL}/auth/callback#")
    (user,) = await _users(sessionmaker)
    assert user.organization_id == two.id


async def test_callback_rejects_state_mismatch(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_org(sessionmaker)
    _start_login(client)
    response = _callback(client, oidc_env, "not-the-state")
    assert response.headers["location"] == f"{FRONTEND_URL}/login?error=state_mismatch"
    assert idp.token_requests == []


def test_callback_without_flow_cookie(client: TestClient) -> None:
    response = client.get("/api/v1/auth/oidc/callback", params={"code": "x", "state": "y"})
    assert response.headers["location"] == f"{FRONTEND_URL}/login?error=flow_expired"


def test_callback_with_provider_error(client: TestClient) -> None:
    _start_login(client)
    response = client.get(
        "/api/v1/auth/oidc/callback", params={"error": "access_denied", "state": "whatever"}
    )
    assert response.headers["location"] == f"{FRONTEND_URL}/login?error=provider_denied"


async def test_callback_rejects_wrong_nonce(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_org(sessionmaker)
    idp.claims = {"sub": "s", "email": "p@example.com"}
    idp.overrides["nonce"] = "replayed-nonce"
    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert response.headers["location"] == f"{FRONTEND_URL}/login?error=invalid_id_token"
    assert await _users(sessionmaker) == []


@pytest.mark.parametrize("override", [{"aud": "someone-else"}, {"iss": "https://evil.test"}])
async def test_callback_rejects_wrong_audience_or_issuer(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
    override: dict[str, str],
) -> None:
    await _seed_org(sessionmaker)
    idp.claims = {"sub": "s", "email": "p@example.com"}
    idp.overrides.update(override)
    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert response.headers["location"] == f"{FRONTEND_URL}/login?error=invalid_id_token"


async def test_callback_refetches_jwks_on_unknown_kid(
    client: TestClient,
    oidc_env: Settings,
    oidc_client: OidcClient,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_org(sessionmaker)
    idp.claims = {"sub": "s", "email": "p@example.com"}
    # Warm the cache, then the provider rolls its key id.
    await oidc_client.jwks()
    idp.jwks["keys"][0]["kid"] = "rotated-key"
    idp.overrides["kid"] = "rotated-key"
    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert response.headers["location"].startswith(f"{FRONTEND_URL}/auth/callback#")
    assert idp.jwks_fetches == 2


async def test_callback_tolerates_provider_clock_skew(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_org(sessionmaker)
    # The provider's clock runs 30 s ahead: `iat` is in our future.
    idp.claims = {"sub": "s", "email": "p@example.com", "iat": int(time.time()) + 30}
    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert response.headers["location"].startswith(f"{FRONTEND_URL}/auth/callback#")


async def test_callback_accepts_a_token_without_kid(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_org(sessionmaker)
    idp.claims = {"sub": "s", "email": "p@example.com"}
    idp.overrides["kid"] = None
    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert response.headers["location"].startswith(f"{FRONTEND_URL}/auth/callback#")


async def test_callback_rejects_an_hs256_forgery(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # Algorithm confusion: HMAC-signed claims must never pass as the provider's.
    await _seed_org(sessionmaker)
    idp.claims = {"sub": "s", "email": "p@example.com"}
    idp.overrides["hs256_secret"] = "a" * 32
    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert response.headers["location"] == f"{FRONTEND_URL}/login?error=invalid_id_token"


async def test_callback_ignores_encryption_keys(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # The right key, but published for encryption only: not a signing key.
    await _seed_org(sessionmaker)
    idp.claims = {"sub": "s", "email": "p@example.com"}
    idp.jwks["keys"][0]["use"] = "enc"
    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert response.headers["location"] == f"{FRONTEND_URL}/login?error=invalid_id_token"


async def test_callback_token_endpoint_refusal(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_org(sessionmaker)
    idp.token_status = 400
    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert response.headers["location"] == f"{FRONTEND_URL}/login?error=token_exchange_failed"


async def test_callback_without_email_claim(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_org(sessionmaker)
    idp.claims = {"sub": "s", "name": "No Mail"}
    state = _start_login(client)
    response = _callback(client, oidc_env, state)
    assert response.headers["location"] == f"{FRONTEND_URL}/login?error=no_email"


# --- Pure helpers ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        (None, "/"),
        ("", "/"),
        ("/projects/p1", "/projects/p1"),
        ("projects", "/"),
        ("//evil.example/x", "/"),
        ("https://evil.example", "/"),
        ("/\\evil.example", "/"),
    ],
)
def test_safe_next_path(candidate: str | None, expected: str) -> None:
    assert safe_next_path(candidate) == expected


def test_identity_from_claims_fallbacks() -> None:
    entra = identity_from_claims(
        {
            "sub": "s",
            "preferred_username": "Ada@Example.com",
            "given_name": "Ada",
            "family_name": "L",
        }
    )
    assert entra == Identity(subject="s", email="ada@example.com", display_name="Ada L")
    bare = identity_from_claims({"sub": "s", "upn": "bob@example.com"})
    assert bare.display_name == "bob"
    # Only the `email` claim can be verified; Cognito sends the flag as a string.
    assert not entra.email_verified
    assert identity_from_claims(
        {"sub": "s", "email": "c@x.io", "email_verified": "true"}
    ).email_verified
    assert not identity_from_claims(
        {"sub": "s", "preferred_username": "c@x.io", "email_verified": True}
    ).email_verified
    with pytest.raises(OidcError) as excinfo:
        identity_from_claims({"sub": "s", "preferred_username": "no-at-sign"})
    assert excinfo.value.code == "no_email"
    with pytest.raises(OidcError) as excinfo:
        identity_from_claims({"email": "x@example.com"})
    assert excinfo.value.code == "invalid_id_token"


def test_flow_cookie_round_trip_and_tampering(oidc_env: Settings) -> None:
    flow = new_flow("/x")
    sealed = encode_flow(flow, oidc_env)
    assert decode_flow(sealed, oidc_env) == flow

    with pytest.raises(OidcError) as excinfo:
        decode_flow(sealed[:-3] + "abc", oidc_env)
    assert excinfo.value.code == "flow_expired"

    # An access token is not a flow cookie, even though both are HS256 JWTs.
    from app.core.security import create_access_token

    with pytest.raises(OidcError):
        decode_flow(create_access_token("user"), oidc_env)


def test_new_flows_are_distinct() -> None:
    first, second = new_flow(None), new_flow(None)
    assert first.state != second.state
    assert first.nonce != second.nonce
    assert first.code_verifier != second.code_verifier


async def test_metadata_is_cached(oidc_client: OidcClient, idp: FakeIdp) -> None:
    first = await oidc_client.metadata()
    idp.discovery_status = 500
    assert await oidc_client.metadata() is first


async def test_client_is_disabled_without_issuer() -> None:
    get_settings.cache_clear()
    client = OidcClient(get_settings())
    assert not client.enabled
    with pytest.raises(OidcError) as excinfo:
        await client.metadata()
    assert excinfo.value.code == "sso_disabled"


def test_get_oidc_client_follows_settings(oidc_env: Settings) -> None:
    first = get_oidc_client()
    assert first is get_oidc_client()
    get_settings.cache_clear()
    assert get_oidc_client() is not first


def test_settings_require_complete_oidc_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_OIDC_ISSUER", ISSUER)
    monkeypatch.delenv("APP_OIDC_CLIENT_ID", raising=False)
    monkeypatch.delenv("APP_OIDC_REDIRECT_URI", raising=False)
    with pytest.raises(ValueError, match="APP_OIDC_CLIENT_ID"):
        Settings()


def test_flow_state_is_immutable() -> None:
    flow = FlowState(state="s", nonce="n", code_verifier="v", next_path="/")
    with pytest.raises(AttributeError):
        flow.state = "other"  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Silent re-authentication and RP-initiated logout
# --------------------------------------------------------------------------- #


def test_login_passes_prompt_none_only_when_asked(client: TestClient) -> None:
    silent = client.get("/api/v1/auth/oidc/login", params={"prompt": "none"})
    assert parse_qs(urlparse(silent.headers["location"]).query)["prompt"] == ["none"]
    normal = client.get("/api/v1/auth/oidc/login")
    assert "prompt" not in parse_qs(urlparse(normal.headers["location"]).query)
    assert client.get("/api/v1/auth/oidc/login", params={"prompt": "login"}).status_code == 422


@pytest.mark.parametrize("error", ["login_required", "interaction_required", "consent_required"])
def test_a_silent_sign_in_without_a_session_is_login_required(
    client: TestClient, error: str
) -> None:
    _start_login(client)
    response = client.get("/api/v1/auth/oidc/callback", params={"error": error, "state": "s"})
    assert response.headers["location"] == f"{FRONTEND_URL}/login?error=login_required"


async def test_sso_tokens_carry_the_sso_claim(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_org(sessionmaker)
    idp.claims = {"sub": "entra-sso", "email": "sso@example.com"}
    state = _start_login(client)
    fragment = _fragment(_callback(client, oidc_env, state))
    assert decode_token(fragment["access_token"])["sso"] is True


def test_logout_redirects_to_the_providers_end_session_endpoint(client: TestClient) -> None:
    response = client.get("/api/v1/auth/oidc/logout")
    assert response.status_code == 303
    location = urlparse(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == (
        f"{ISSUER}/protocol/openid-connect/logout"
    )
    query = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert query == {"client_id": CLIENT_ID, "post_logout_redirect_uri": f"{FRONTEND_URL}/login"}


def test_logout_without_end_session_or_provider_goes_to_login(
    client: TestClient, idp: FakeIdp
) -> None:
    idp.end_session = False
    assert client.get("/api/v1/auth/oidc/logout").headers["location"] == f"{FRONTEND_URL}/login"


def test_logout_when_the_provider_is_down_still_lands_on_login(
    client: TestClient, idp: FakeIdp
) -> None:
    idp.discovery_status = 503
    assert client.get("/api/v1/auth/oidc/logout").headers["location"] == f"{FRONTEND_URL}/login"


async def test_callback_syncs_groups_into_memberships_and_admin_flag(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTH-3: the groups claim grants project roles and superuser at sign-in."""
    monkeypatch.setenv("APP_OIDC_ADMIN_GROUPS", "platform-admins")
    get_settings.cache_clear()
    org = await _seed_org(sessionmaker)
    async with sessionmaker() as session:
        project = Project(
            organization_id=org.id,
            name="Roads",
            workflow={},
            settings={"idp_groups": {"labelers": "annotator", "leads": "reviewer"}},
        )
        session.add(project)
        await session.commit()
        project_id = project.id
    idp.claims = {
        "sub": "sub-g",
        "email": "g@example.com",
        "groups": ["labelers", "leads", "platform-admins"],
    }

    response = _callback(client, get_settings(), _start_login(client))

    assert response.status_code == 303, response.text
    claims = decode_token(_fragment(response)["access_token"])
    async with sessionmaker() as session:
        user = await session.scalar(select(User).where(User.email == "g@example.com"))
        assert user is not None
        assert user.is_superuser is True
        membership = await session.scalar(select(Membership).where(Membership.user_id == user.id))
        assert membership is not None
        assert membership.project_id == project_id
        assert membership.role == ProjectRole.REVIEWER
        assert membership.source == MembershipSource.IDP
    assert claims["sub"] == str(user.id)
    actions = await _audit_actions(sessionmaker)
    assert "membership.create" in actions
    assert "user.superuser_sync" in actions


async def test_callback_leaves_groups_to_scim_once_it_pushes_them(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """AUTH-3: with SCIM groups in the organisation, the token's groups are ignored."""
    org = await _seed_org(sessionmaker)
    async with sessionmaker() as session:
        session.add(
            Project(
                organization_id=org.id,
                name="Roads",
                workflow={},
                settings={"idp_groups": {"labelers": "annotator"}},
            )
        )
        session.add(ScimGroup(organization_id=org.id, display_name="pushed"))
        await session.commit()
    idp.claims = {"sub": "sub-s", "email": "s@example.com", "groups": ["labelers"]}

    response = _callback(client, get_settings(), _start_login(client))

    assert response.status_code == 303, response.text
    async with sessionmaker() as session:
        assert await session.scalar(select(Membership)) is None


async def test_entra_shaped_tokens_sync_by_group_object_id(
    client: TestClient,
    oidc_env: Settings,
    idp: FakeIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AUTH-3 against tokens as Entra ID v2 issues them.

    No `email` claim unless configured (the address is in
    `preferred_username`), groups as object ids, and — past 200 groups — no
    `groups` at all but `_claim_names` pointing at Microsoft Graph.
    """
    labelers = "5b1c3f0e-8d2a-4c5e-9f11-2a3b4c5d6e7f"
    admins = "c0ffee00-1111-4222-8333-444455556666"
    monkeypatch.setenv("APP_OIDC_ADMIN_GROUPS", admins)
    get_settings.cache_clear()
    org = await _seed_org(sessionmaker)
    other_admin = await _add_user(sessionmaker, org, email="root@example.com")
    async with sessionmaker() as session:
        root = await session.get(User, other_admin.id)
        assert root is not None
        root.is_superuser = True
        project = Project(
            organization_id=org.id,
            name="Invoices",
            workflow={},
            settings={"idp_groups": {labelers: "annotator"}},
        )
        session.add(project)
        await session.commit()
        project_id = project.id
    entra = {
        "sub": "AAAAAAAAAAAAAAAAAAAAAIkzqFVrSaSaFHy782bbtaQ",  # pairwise, per app
        "oid": "00000000-0000-0000-66f3-3332eca7ea81",
        "tid": "9188040d-6c67-4c5b-b112-36a304b66dad",
        "preferred_username": "Anna.Virtanen@contoso.onmicrosoft.com",
        "name": "Anna Virtanen",
        "ver": "2.0",
    }

    async def state() -> tuple[ProjectRole | None, bool]:
        async with sessionmaker() as session:
            user = await session.scalar(
                select(User).where(User.email == "anna.virtanen@contoso.onmicrosoft.com")
            )
            assert user is not None
            membership = await session.scalar(
                select(Membership).where(
                    Membership.user_id == user.id, Membership.project_id == project_id
                )
            )
            return (membership.role if membership else None), user.is_superuser

    idp.claims = {**entra, "groups": [labelers, admins, "ffffffff-0000-0000-0000-000000000000"]}
    assert _callback(client, get_settings(), _start_login(client)).status_code == 303
    assert await state() == (ProjectRole.ANNOTATOR, True)

    # Group overage: the groups are behind Graph, so nothing changes.
    idp.claims = {
        **entra,
        "_claim_names": {"groups": "src1"},
        "_claim_sources": {
            "src1": {"endpoint": "https://graph.microsoft.com/v1.0/users/x/getMemberObjects"}
        },
    }
    assert _callback(client, get_settings(), _start_login(client)).status_code == 303
    assert await state() == (ProjectRole.ANNOTATOR, True)

    # Removed from every group: Entra omits the claim, and access goes.
    idp.claims = dict(entra)
    assert _callback(client, get_settings(), _start_login(client)).status_code == 303
    assert await state() == (None, False)
