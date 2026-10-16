"""Tests for SAML 2.0 single sign-on (AUTH-1): `services/saml.py` and the
`/auth/saml/login`, `/auth/saml/acs` and `/auth/saml/metadata` routes.

The identity provider is a real pysaml2 IdP with a certificate generated per
module, and signatures are made and checked by the real `xmlsec1`, so these
tests exercise the same verification production does. Like
`test_api_oidc.py`, the module uses in-memory SQLite with `CITEXT` as
`VARCHAR`.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI
from fastapi.testclient import TestClient
from saml2 import BINDING_HTTP_REDIRECT, samlp
from saml2.config import IdPConfig
from saml2.metadata import create_metadata_string
from saml2.saml import (
    NAMEID_FORMAT_EMAILADDRESS,
    NAMEID_FORMAT_PERSISTENT,
    NAMEID_FORMAT_TRANSIENT,
    NameID,
)
from saml2.server import Server
from saml2.sigver import get_xmlsec_binary
from sqlalchemy import Table, select
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.core.config import Settings, get_settings
from app.core.security import decode_token
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import (
    AuditEvent,
    LicenseState,
    Membership,
    Organization,
    Project,
    ScimGroup,
    User,
)
from app.services.saml import (
    MemoryUsedRequests,
    SamlClient,
    SamlError,
    SamlFlow,
    decode_flow,
    encode_flow,
    get_saml_client,
    identity_from_response,
)

IDP_ENTITY = "https://idp.example.test/saml"
IDP_SSO = "https://idp.example.test/sso"
ACS_URL = "http://api.test/api/v1/auth/saml/acs"
SP_ENTITY = "http://api.test/api/v1/auth/saml/metadata"
FRONTEND_URL = "http://app.test"
MS_GROUPS = "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups"
MS_EMAIL = "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"


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


# --- Test identity provider ------------------------------------------------------


def _keypair(directory: Path, name: str) -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    key_file, cert_file = directory / f"{name}.key", directory / f"{name}.pem"
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(key_file), str(cert_file)


def _idp_config(key_file: str, cert_file: str, sp_metadata: str | None) -> IdPConfig:
    config = IdPConfig()
    config.load(
        {
            "entityid": IDP_ENTITY,
            "key_file": key_file,
            "cert_file": cert_file,
            "xmlsec_binary": get_xmlsec_binary(),
            "service": {
                "idp": {
                    "endpoints": {"single_sign_on_service": [(IDP_SSO, BINDING_HTTP_REDIRECT)]},
                    "policy": {"default": {"lifetime": {"minutes": 5}}},
                },
            },
            "metadata": {"inline": [sp_metadata]} if sp_metadata else {},
        }
    )
    return config


class SamlIdp:
    """A real SAML IdP: publishes metadata, signs responses with its own key."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.key_file, self.cert_file = _keypair(directory, "idp")
        metadata: bytes = create_metadata_string(
            None, config=_idp_config(self.key_file, self.cert_file, None), sign=False
        )
        self.metadata = metadata.decode()
        self._server: Server | None = None

    def trust(self, sp_metadata: str) -> None:
        self._server = Server(config=_idp_config(self.key_file, self.cert_file, sp_metadata))

    def response(
        self,
        request_id: str,
        *,
        name_id: str = "persistent-ada",
        name_id_format: str = NAMEID_FORMAT_PERSISTENT,
        attributes: dict[str, list[str]] | None = None,
        sign: bool = True,
        server: Server | None = None,
    ) -> str:
        """A base64 SAMLResponse answering `request_id`, as the browser would post it."""
        idp = server or self._server
        assert idp is not None, "call trust() first"
        if attributes is None:
            attributes = {"mail": ["ada@acme.example"], "displayName": ["Ada Lovelace"]}
        response = idp.create_authn_response(
            identity=attributes,
            in_response_to=request_id,
            destination=ACS_URL,
            sp_entity_id=SP_ENTITY,
            name_id=NameID(format=name_id_format, text=name_id),
            userid=name_id,
            authn={"class_ref": "urn:oasis:names:tc:SAML:2.0:ac:classes:Password"},
            sign_assertion=sign,
            sign_response=False,
        )
        return base64.b64encode(str(response).encode()).decode()

    def error_response(self, request_id: str) -> str:
        assert self._server is not None
        response = self._server.create_error_response(
            request_id, ACS_URL, (samlp.STATUS_AUTHN_FAILED, "user cancelled")
        )
        return base64.b64encode(str(response).encode()).decode()

    def impostor(self, sp_metadata: str) -> Server:
        """Same entity id, a key the metadata does not name."""
        key_file, cert_file = _keypair(self.directory, "impostor")
        return Server(config=_idp_config(key_file, cert_file, sp_metadata))


@pytest.fixture(scope="module")
def idp(tmp_path_factory: pytest.TempPathFactory) -> SamlIdp:
    return SamlIdp(tmp_path_factory.mktemp("idp"))


@pytest.fixture
def saml_env(monkeypatch: pytest.MonkeyPatch, idp: SamlIdp, tmp_path: Path) -> Iterator[Settings]:
    metadata = tmp_path / "idp-metadata.xml"
    metadata.write_text(idp.metadata)
    monkeypatch.setenv("APP_SAML_IDP_METADATA_FILE", str(metadata))
    monkeypatch.setenv("APP_SAML_ACS_URL", ACS_URL)
    monkeypatch.setenv("APP_SAML_DISPLAY_NAME", "Acme SAML")
    monkeypatch.setenv("APP_FRONTEND_URL", FRONTEND_URL)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
async def saml_client(saml_env: Settings, idp: SamlIdp) -> SamlClient:
    client = SamlClient(saml_env, used=MemoryUsedRequests())
    idp.trust(await client.sp_metadata())
    return client


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
    saml_env: Settings, saml_client: SamlClient, sessionmaker: async_sessionmaker[AsyncSession]
) -> FastAPI:
    application = create_app()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            yield session

    application.dependency_overrides[get_session] = _get_session
    application.dependency_overrides[get_saml_client] = lambda: saml_client
    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False, follow_redirects=False)


async def _seed_org(sessionmaker: async_sessionmaker[AsyncSession]) -> Organization:
    async with sessionmaker() as session:
        org = Organization(name="Acme", slug="acme")
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
) -> User:
    async with sessionmaker() as session:
        user = User(
            organization_id=organization.id,
            email=email,
            display_name="Existing Person",
            idp_subject=idp_subject,
            password_hash="argon2-placeholder",
            is_active=True,
            is_superuser=False,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _users(sessionmaker: async_sessionmaker[AsyncSession]) -> list[User]:
    async with sessionmaker() as session:
        return list((await session.scalars(select(User))).all())


async def _audit(sessionmaker: async_sessionmaker[AsyncSession]) -> list[tuple[str, Any]]:
    async with sessionmaker() as session:
        rows = await session.scalars(select(AuditEvent).order_by(AuditEvent.created_at))
        return [(row.action, row.after) for row in rows]


def _start_login(client: TestClient, settings: Settings, next_path: str | None = None) -> str:
    """Hit the login route; return the AuthnRequest id sealed in the flow cookie."""
    params = {"next": next_path} if next_path else {}
    response = client.get("/api/v1/auth/saml/login", params=params)
    assert response.status_code == 303, response.text
    assert response.headers["location"].startswith(IDP_SSO + "?SAMLRequest=")
    cookie = client.cookies.get("saml_flow")
    assert cookie is not None
    return decode_flow(cookie, settings).request_id


def _post(client: TestClient, saml_response: str) -> httpx.Response:
    return client.post("/api/v1/auth/saml/acs", data={"SAMLResponse": saml_response})


def _error(response: httpx.Response) -> str:
    assert response.status_code == 303, response.text
    location = urlparse(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{FRONTEND_URL}/login"
    return parse_qs(location.query)["error"][0]


def _fragment(response: httpx.Response) -> dict[str, str]:
    assert response.status_code == 303, response.text
    location = urlparse(response.headers["location"])
    assert location.path == "/auth/callback"
    return {k: v[0] for k, v in parse_qs(location.fragment).items()}


# --- /auth/providers and /auth/saml/metadata --------------------------------------


def test_providers_lists_saml_when_configured(client: TestClient) -> None:
    body = client.get("/api/v1/auth/providers").json()
    assert body["saml"] == {"display_name": "Acme SAML", "login_path": "/auth/saml/login"}
    assert body["oidc"] is None


def test_saml_routes_are_404_when_not_configured(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    get_settings.cache_clear()
    client = TestClient(create_app(), raise_server_exceptions=False, follow_redirects=False)
    assert client.get("/api/v1/auth/providers").json()["saml"] is None
    assert client.get("/api/v1/auth/saml/login").status_code == 404
    assert client.get("/api/v1/auth/saml/metadata").status_code == 404
    assert client.post("/api/v1/auth/saml/acs", data={"SAMLResponse": "x"}).status_code == 404


def test_metadata_names_the_entity_and_the_acs(client: TestClient) -> None:
    response = client.get("/api/v1/auth/saml/metadata")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/samlmetadata+xml")
    assert f'entityID="{SP_ENTITY}"' in response.text
    assert f'Location="{ACS_URL}"' in response.text
    assert "HTTP-POST" in response.text


# --- /auth/saml/login ----------------------------------------------------------------


def test_login_sets_a_lax_cookie_over_plain_http(client: TestClient, saml_env: Settings) -> None:
    _start_login(client, saml_env, "/projects/1")
    header = client.get("/api/v1/auth/saml/login").headers["set-cookie"]
    assert "HttpOnly" in header
    assert "Path=/api/v1/auth/saml" in header
    assert "SameSite=lax" in header
    assert decode_flow(client.cookies["saml_flow"], saml_env).next_path == "/"


def test_login_cookie_is_samesite_none_over_https(app: FastAPI) -> None:
    # The IdP posts the response cross-site, which only a SameSite=None cookie survives.
    client = TestClient(app, base_url="https://api.test", follow_redirects=False)
    header = client.get("/api/v1/auth/saml/login").headers["set-cookie"]
    assert "SameSite=none" in header
    assert "Secure" in header
    # And it is expired with the same attributes, or a browser may keep it.
    cleared = client.post("/api/v1/auth/saml/acs", data={}).headers["set-cookie"]
    assert "saml_flow=" in cleared
    assert "SameSite=none" in cleared
    assert "Secure" in cleared


def test_login_keeps_the_next_path_on_this_site(client: TestClient, saml_env: Settings) -> None:
    _start_login(client, saml_env, "//evil.example/x")
    assert decode_flow(client.cookies["saml_flow"], saml_env).next_path == "/"
    _start_login(client, saml_env, "/projects/7")
    assert decode_flow(client.cookies["saml_flow"], saml_env).next_path == "/projects/7"


# --- /auth/saml/acs ----------------------------------------------------------------


async def test_a_signed_response_provisions_a_new_user(
    client: TestClient,
    saml_env: Settings,
    idp: SamlIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_org(sessionmaker)
    request_id = _start_login(client, saml_env, "/projects/7")

    fragment = _fragment(_post(client, idp.response(request_id)))

    assert fragment["next"] == "/projects/7"
    claims = decode_token(fragment["access_token"])
    assert claims["email"] == "ada@acme.example"
    # No `sso` claim: the frontend's silent renewal and IdP sign-out are OIDC's.
    assert "sso" not in claims
    [user] = await _users(sessionmaker)
    assert (user.email, user.display_name) == ("ada@acme.example", "Ada Lovelace")
    assert user.idp_subject == "saml:persistent-ada"
    assert user.password_hash is None
    assert await _audit(sessionmaker) == [
        ("user.provision", {"email": "ada@acme.example", "method": "saml"}),
        ("auth.login", {"method": "saml"}),
    ]
    assert "saml_flow" not in client.cookies


async def test_the_next_sign_in_finds_the_user_by_subject(
    client: TestClient,
    saml_env: Settings,
    idp: SamlIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_org(sessionmaker)
    _fragment(_post(client, idp.response(_start_login(client, saml_env))))
    renamed = {"mail": ["ada.l@acme.example"], "displayName": ["Ada"]}
    _fragment(_post(client, idp.response(_start_login(client, saml_env), attributes=renamed)))
    assert len(await _users(sessionmaker)) == 1


async def test_an_existing_account_is_linked_only_when_emails_are_trusted(
    client: TestClient,
    saml_env: Settings,
    idp: SamlIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org = await _seed_org(sessionmaker)
    existing = await _add_user(sessionmaker, org, email="ada@acme.example")

    refused = _post(client, idp.response(_start_login(client, saml_env)))
    assert _error(refused) == "email_unverified"

    monkeypatch.setattr(saml_env, "saml_trust_email", True)
    _fragment(_post(client, idp.response(_start_login(client, saml_env))))
    [user] = await _users(sessionmaker)
    assert user.id == existing.id
    assert user.idp_subject == "saml:persistent-ada"


async def test_admin_groups_from_a_uri_named_attribute(
    client: TestClient,
    saml_env: Settings,
    idp: SamlIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Entra ID names attributes by URI; `groups` matches the last segment.
    monkeypatch.setattr(saml_env, "saml_admin_groups", "platform-admins")
    await _seed_org(sessionmaker)
    attributes = {
        MS_EMAIL: ["grace@acme.example"],
        MS_GROUPS: ["annotators", "platform-admins"],
    }
    _fragment(_post(client, idp.response(_start_login(client, saml_env), attributes=attributes)))
    [user] = await _users(sessionmaker)
    assert user.email == "grace@acme.example"
    assert user.is_superuser is True


async def test_auto_provisioning_off_refuses_an_unknown_user(
    client: TestClient,
    saml_env: Settings,
    idp: SamlIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(saml_env, "saml_auto_provision", False)
    await _seed_org(sessionmaker)
    assert _error(_post(client, idp.response(_start_login(client, saml_env)))) == (
        "not_provisioned"
    )


async def test_a_response_is_accepted_once(
    client: TestClient,
    saml_env: Settings,
    idp: SamlIdp,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    # A bearer assertion is single-use: a captured cookie and response pair
    # must not sign in again within the cookie's lifetime.
    await _seed_org(sessionmaker)
    request_id = _start_login(client, saml_env)
    cookie = client.cookies["saml_flow"]
    saml_response = idp.response(request_id)
    _fragment(_post(client, saml_response))
    client.cookies.set("saml_flow", cookie, path="/api/v1/auth/saml")
    assert _error(_post(client, saml_response)) == "flow_expired"


class TestRefusals:
    async def test_a_tampered_response(
        self,
        client: TestClient,
        saml_env: Settings,
        idp: SamlIdp,
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_org(sessionmaker)
        signed = base64.b64decode(idp.response(_start_login(client, saml_env))).decode()
        forged = signed.replace("ada@acme.example", "eve@acme.example")
        assert forged != signed
        response = _post(client, base64.b64encode(forged.encode()).decode())
        assert _error(response) == "invalid_response"
        assert await _users(sessionmaker) == []

    async def test_an_unsigned_assertion(
        self,
        client: TestClient,
        saml_env: Settings,
        idp: SamlIdp,
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_org(sessionmaker)
        unsigned = idp.response(_start_login(client, saml_env), sign=False)
        assert _error(_post(client, unsigned)) == "invalid_response"
        assert await _users(sessionmaker) == []

    async def test_a_key_the_metadata_does_not_name(
        self,
        client: TestClient,
        saml_env: Settings,
        idp: SamlIdp,
        saml_client: SamlClient,
        sessionmaker: async_sessionmaker[AsyncSession],
    ) -> None:
        await _seed_org(sessionmaker)
        impostor = idp.impostor(await saml_client.sp_metadata())
        forged = idp.response(_start_login(client, saml_env), server=impostor)
        assert _error(_post(client, forged)) == "invalid_response"
        assert await _users(sessionmaker) == []

    def test_a_response_to_another_request(
        self, client: TestClient, saml_env: Settings, idp: SamlIdp
    ) -> None:
        _start_login(client, saml_env)
        # IdP-initiated, or replayed into another browser's flow: a login-CSRF.
        assert _error(_post(client, idp.response("id-not-this-flow"))) == "state_mismatch"

    def test_no_flow_cookie(self, client: TestClient, saml_env: Settings, idp: SamlIdp) -> None:
        request_id = _start_login(client, saml_env)
        client.cookies.clear()
        assert _error(_post(client, idp.response(request_id))) == "flow_expired"

    def test_no_saml_response(self, client: TestClient, saml_env: Settings) -> None:
        _start_login(client, saml_env)
        assert _error(client.post("/api/v1/auth/saml/acs", data={})) == "invalid_response"

    def test_not_base64_or_too_large(self, client: TestClient, saml_env: Settings) -> None:
        _start_login(client, saml_env)
        assert _error(_post(client, "<xml/>")) == "invalid_response"
        _start_login(client, saml_env)
        assert _error(_post(client, "A" * (300 * 1024))) == "invalid_response"

    def test_an_idp_error_status(
        self, client: TestClient, saml_env: Settings, idp: SamlIdp
    ) -> None:
        response = _post(client, idp.error_response(_start_login(client, saml_env)))
        assert _error(response) == "provider_denied"

    def test_a_transient_name_id(
        self, client: TestClient, saml_env: Settings, idp: SamlIdp
    ) -> None:
        transient = idp.response(
            _start_login(client, saml_env), name_id="t-1", name_id_format=NAMEID_FORMAT_TRANSIENT
        )
        assert _error(_post(client, transient)) == "unsupported_name_id"


def test_licence_without_sso_refuses_saml(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.v1 import auth as auth_mod

    monkeypatch.setattr(auth_mod, "has_feature", lambda _licence, _feature: False)
    client = TestClient(app, raise_server_exceptions=False, follow_redirects=False)
    assert client.get("/api/v1/auth/providers").json()["saml"] is None
    assert _error(client.get("/api/v1/auth/saml/login")) == "license_feature"
    assert _error(_post(client, "x")) == "license_feature"


# --- Metadata sources ----------------------------------------------------------------


async def test_metadata_from_a_url_survives_a_failed_refresh(
    saml_env: Settings, idp: SamlIdp, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    up = True

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=idp.metadata) if up else httpx.Response(503)

    monkeypatch.setattr(saml_env, "saml_idp_metadata_file", None)
    monkeypatch.setattr(saml_env, "saml_idp_metadata_url", "https://idp.example.test/metadata")
    client = SamlClient(saml_env, http=httpx.AsyncClient(transport=httpx.MockTransport(handle)))

    location, _ = await client.login_url(None)
    assert location.startswith(IDP_SSO)
    up = False
    client._metadata_at = 0.0  # due for a refresh
    location, _ = await client.login_url(None)
    assert location.startswith(IDP_SSO)
    assert len(calls) == 2
    # The failure is not retried on every sign-in while the IdP is down.
    await client.login_url(None)
    assert len(calls) == 2


def test_production_needs_https_metadata(saml_env: Settings) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="must be https"):
        Settings(
            env="production",
            secret_key="x" * 40,
            saml_idp_metadata_url="http://idp.example.test/metadata",
            saml_idp_metadata_file=None,
            saml_acs_url=ACS_URL,
        )


async def test_unreachable_metadata_is_provider_unreachable(
    client: TestClient, saml_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(saml_env, "saml_idp_metadata_file", "/nonexistent/idp.xml")
    saml = SamlClient(saml_env)
    with pytest.raises(SamlError) as raised:
        await saml.login_url(None)
    assert raised.value.code == "provider_unreachable"


async def test_metadata_naming_another_idp_is_no_idp(
    saml_env: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(saml_env, "saml_idp_entity_id", "https://other.example/saml")
    with pytest.raises(SamlError) as raised:
        await SamlClient(saml_env).login_url(None)
    assert raised.value.code == "no_idp"


# --- Units ---------------------------------------------------------------------------


class TestIdentityFromResponse:
    def test_uri_named_attributes(self, saml_env: Settings) -> None:
        identity = identity_from_response(
            "abc",
            NAMEID_FORMAT_PERSISTENT,
            {
                MS_EMAIL: ["Ada@Acme.Example"],
                "http://schemas.microsoft.com/identity/claims/displayname": ["Ada L"],
                MS_GROUPS: ["g1", "g2"],
            },
            saml_env,
        )
        assert (identity.subject, identity.email, identity.display_name) == (
            "saml:abc",
            "ada@acme.example",
            "Ada L",
        )
        assert identity.groups == ("g1", "g2")
        assert identity.email_verified is False

    def test_fallbacks(self, saml_env: Settings) -> None:
        by_name_id = identity_from_response(
            "grace@acme.example",
            NAMEID_FORMAT_EMAILADDRESS,
            {"givenName": ["Grace"], "sn": ["Hopper"]},
            saml_env,
        )
        assert (by_name_id.email, by_name_id.display_name) == (
            "grace@acme.example",
            "Grace Hopper",
        )
        # No groups attribute: memberships are left alone, not emptied.
        assert by_name_id.groups is None
        bare = identity_from_response("x", None, {"mail": ["x@acme.example"]}, saml_env)
        assert bare.display_name == "x"

    def test_refusals(self, saml_env: Settings) -> None:
        with pytest.raises(SamlError, match="NameID"):
            identity_from_response(None, None, {}, saml_env)
        with pytest.raises(SamlError) as raised:
            identity_from_response("x", NAMEID_FORMAT_PERSISTENT, {"mail": ["nope"]}, saml_env)
        assert raised.value.code == "no_email"

    def test_a_long_name_id_is_hashed_into_the_column(self, saml_env: Settings) -> None:
        identity = identity_from_response("n" * 400, None, {"mail": ["a@b.example"]}, saml_env)
        assert identity.subject.startswith("saml:sha256:")
        assert len(identity.subject) <= 255

    def test_group_sync_can_be_off(
        self, saml_env: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(saml_env, "saml_groups_attribute", "")
        identity = identity_from_response(
            "x", None, {"mail": ["a@b.example"], "groups": ["g"]}, saml_env
        )
        assert identity.groups is None


class TestFlowCookie:
    def test_round_trip_and_key_rotation(
        self, saml_env: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sealed = encode_flow(SamlFlow(request_id="id-1", next_path="/p"), saml_env)
        assert decode_flow(sealed, saml_env) == SamlFlow("id-1", "/p")
        old_key = saml_env.secret_key
        monkeypatch.setattr(saml_env, "secret_key", "a-new-secret-key-of-enough-length-123")
        monkeypatch.setattr(saml_env, "secret_key_previous", old_key)
        assert decode_flow(sealed, saml_env).request_id == "id-1"

    def test_refuses_a_foreign_or_expired_token(self, saml_env: Settings) -> None:
        import jwt

        assert saml_env.secret_key
        oidc_like = jwt.encode({"typ": "oidc_flow"}, saml_env.secret_key, algorithm="HS256")
        expired = jwt.encode(
            {"typ": "saml_flow", "rid": "x", "exp": datetime.now(UTC) - timedelta(minutes=1)},
            saml_env.secret_key,
            algorithm="HS256",
        )
        unsigned = jwt.encode({"typ": "saml_flow", "rid": "x"}, "another-key", algorithm="HS256")
        for token in (oidc_like, expired, unsigned):
            with pytest.raises(SamlError) as raised:
                decode_flow(token, saml_env)
            assert raised.value.code == "flow_expired"


class TestAddressedToUs:
    """The checks pysaml2 leaves optional: issuer, audience, recipient, destination."""

    @staticmethod
    def _response(
        *,
        response_issuer: str = IDP_ENTITY,
        assertion_issuer: str = IDP_ENTITY,
        audiences: list[str] | None = None,
        recipient: str | None = ACS_URL,
        destination: str | None = ACS_URL,
        conditions: bool = True,
    ) -> Any:
        restriction = SimpleNamespace(
            audience=[SimpleNamespace(text=a) for a in (audiences or [SP_ENTITY])]
        )
        assertion = SimpleNamespace(
            issuer=SimpleNamespace(text=assertion_issuer),
            conditions=SimpleNamespace(audience_restriction=[restriction]) if conditions else None,
            subject=SimpleNamespace(
                subject_confirmation=[
                    SimpleNamespace(subject_confirmation_data=SimpleNamespace(recipient=recipient))
                ]
            ),
        )
        return SimpleNamespace(
            issuer=lambda: response_issuer,
            assertion=assertion,
            response=SimpleNamespace(destination=destination),
        )

    def test_accepts_what_is_addressed_to_us(self, saml_env: Settings) -> None:
        SamlClient(saml_env)._check_addressed_to_us(self._response(), IDP_ENTITY)
        # Destination is optional in the profile when the response is unsigned.
        SamlClient(saml_env)._check_addressed_to_us(self._response(destination=None), IDP_ENTITY)

    @pytest.mark.parametrize(
        "change",
        [
            {"assertion_issuer": "https://other-idp.example/saml"},
            {"response_issuer": "https://other-idp.example/saml"},
            {"conditions": False},
            {"audiences": ["https://another-sp.example"]},
            {"recipient": "https://evil.example/acs"},
            {"recipient": None},
            {"destination": "https://evil.example/acs"},
        ],
    )
    def test_refuses_anything_else(self, saml_env: Settings, change: dict[str, Any]) -> None:
        with pytest.raises(SamlError) as raised:
            SamlClient(saml_env)._check_addressed_to_us(self._response(**change), IDP_ENTITY)
        assert raised.value.code == "invalid_response"
