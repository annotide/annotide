"""OIDC single sign-on (AUTH-1).

Authorization Code flow with PKCE against any OpenID Connect provider that
publishes discovery metadata: Entra ID, Keycloak, Cognito, Google, Okta.
The provider is configured with ``APP_OIDC_*`` (see ``core/config.py``).

The flow, from the browser's point of view:

1. ``GET /auth/oidc/login`` — the API creates ``state``, ``nonce`` and a PKCE
   verifier, seals them into a short-lived signed cookie and redirects to the
   provider's authorization endpoint.
2. The provider authenticates the user and redirects to
   ``GET /auth/oidc/callback?code=…&state=…``.
3. The API checks the state against the cookie, exchanges the code at the
   token endpoint, verifies the ID token against the provider's JWKS
   (signature, issuer, audience, expiry, nonce) and maps the claims onto a
   ``user`` row: by ``idp_subject`` first, then by e-mail (which links an
   existing local account), else provisioning a new one.
4. The API issues its own access token — the same one local login issues —
   and hands it to the frontend in a URL fragment, which browsers never send
   to a server, so it does not land in access logs.

Everything after step 4 is unchanged: the rest of the API only ever sees the
platform's own bearer token.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.security import ALGORITHM, OWN_TOKEN_OPTIONS
from app.models import Organization, User
from app.services import audit
from app.services.licensing import seats
from app.services.licensing.state import effective_license

log = structlog.get_logger(__name__)

#: How long the browser has to complete the round trip through the provider.
FLOW_TTL = timedelta(minutes=10)
#: Provider metadata and keys are re-read after this many seconds.
METADATA_TTL_SECONDS = 3600
#: ID-token signature algorithms accepted. HS* is deliberately excluded: an
#: ID token must be signed with the provider's asymmetric key, never a secret
#: shared with the client.
ID_TOKEN_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256"]
#: Clock skew tolerated on an ID token's `exp` / `iat` / `nbf`: the provider
#: mints it on its own clock, which may run slightly ahead of ours.
ID_TOKEN_LEEWAY_SECONDS = 60

_FLOW_TOKEN_TYPE = "oidc_flow"


class OidcError(Exception):
    """A sign-in attempt that cannot proceed.

    ``code`` is a short, stable identifier the callback forwards to the
    frontend (``/login?error=<code>``); ``detail`` is for the log only, since
    it may name provider internals the user has no use for.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class OidcDisabledError(OidcError):
    """No provider is configured."""

    def __init__(self) -> None:
        super().__init__("sso_disabled", "OIDC is not configured on this installation")


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    """The subset of the discovery document the flow needs."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    #: RP-initiated logout (OpenID Connect RP-Initiated Logout 1.0); optional.
    end_session_endpoint: str | None = None


@dataclass(frozen=True, slots=True)
class FlowState:
    """What the login step remembers until the callback arrives."""

    state: str
    nonce: str
    code_verifier: str
    next_path: str


@dataclass(frozen=True, slots=True)
class Identity:
    """Claims from a verified ID token, normalised for the user table."""

    subject: str
    email: str
    display_name: str
    #: Groups from `APP_OIDC_GROUPS_CLAIM` (AUTH-3); `None` when the token
    #: carries no such claim, which leaves memberships alone.
    groups: tuple[str, ...] | None = None
    #: The provider vouches for `email` (`email_verified`). False for an
    #: address read from `preferred_username` / `upn` or without the claim.
    email_verified: bool = False


# --- Flow cookie -------------------------------------------------------------


def encode_flow(flow: FlowState, settings: Settings) -> str:
    """Seal the flow state into a signed, expiring token for the cookie.

    The state travels in the cookie rather than server-side storage so that
    the login step needs neither Redis nor the database, and so that a
    second API replica can complete a flow the first one started.
    """
    if not settings.secret_key:
        raise RuntimeError("APP_SECRET_KEY must be set to start an OIDC flow")
    now = datetime.now(UTC)
    claims = {
        "typ": _FLOW_TOKEN_TYPE,
        "iat": now,
        "exp": now + FLOW_TTL,
        "state": flow.state,
        "nonce": flow.nonce,
        "cv": flow.code_verifier,
        "next": flow.next_path,
    }
    encoded: str = jwt.encode(claims, settings.secret_key, algorithm=ALGORITHM)
    return encoded


def decode_flow(value: str, settings: Settings) -> FlowState:
    """Reverse :func:`encode_flow`. Raises ``OidcError`` on anything off."""
    if not settings.secret_key:
        raise RuntimeError("APP_SECRET_KEY must be set to complete an OIDC flow")
    keys = [settings.secret_key]
    if settings.secret_key_previous and settings.secret_key_previous != settings.secret_key:
        keys.append(settings.secret_key_previous)  # a rotation mid-flow
    claims: dict[str, Any] | None = None
    for key in keys:
        try:
            claims = jwt.decode(value, key, algorithms=[ALGORITHM], options=OWN_TOKEN_OPTIONS)
            break
        except jwt.InvalidSignatureError as exc:
            failure: jwt.PyJWTError = exc
        except jwt.PyJWTError as exc:
            raise OidcError("flow_expired", f"flow cookie rejected: {exc}") from exc
    if claims is None:
        raise OidcError("flow_expired", f"flow cookie rejected: {failure}")
    if claims.get("typ") != _FLOW_TOKEN_TYPE:
        raise OidcError("flow_expired", "flow cookie has the wrong type")
    try:
        return FlowState(
            state=str(claims["state"]),
            nonce=str(claims["nonce"]),
            code_verifier=str(claims["cv"]),
            next_path=str(claims["next"]),
        )
    except KeyError as exc:
        raise OidcError("flow_expired", f"flow cookie missing {exc}") from exc


def safe_next_path(candidate: str | None) -> str:
    """Keep the post-login redirect on this site.

    Only an absolute path is accepted: ``//evil.example`` and ``https://…``
    are both rejected, since either would turn the callback into an open
    redirect carrying the user's token in its fragment.
    """
    if not candidate or not candidate.startswith("/") or candidate.startswith("//"):
        return "/"
    if "\\" in candidate or candidate.startswith("/\\"):
        return "/"
    return candidate


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def new_flow(next_path: str | None) -> FlowState:
    """Fresh state, nonce and PKCE verifier (RFC 7636)."""
    return FlowState(
        state=secrets.token_urlsafe(32),
        nonce=secrets.token_urlsafe(32),
        code_verifier=_b64url(secrets.token_bytes(48)),
        next_path=safe_next_path(next_path),
    )


# --- Provider client ---------------------------------------------------------


class OidcClient:
    """Talks to the identity provider. One instance per process.

    Discovery metadata and the JWKS are cached for :data:`METADATA_TTL_SECONDS`;
    the JWKS is also refetched once when an ID token names a key id the cache
    does not hold, which is how providers roll keys.
    """

    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._http = http
        self._metadata: ProviderMetadata | None = None
        self._metadata_at = 0.0
        self._jwks: dict[str, Any] | None = None
        self._jwks_at = 0.0

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def enabled(self) -> bool:
        return self._settings.oidc_enabled

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        return self._http

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise OidcDisabledError()

    async def metadata(self) -> ProviderMetadata:
        """The provider's discovery document, cached."""
        self._require_enabled()
        if self._metadata is not None and time.monotonic() - self._metadata_at < (
            METADATA_TTL_SECONDS
        ):
            return self._metadata
        issuer = str(self._settings.oidc_issuer).rstrip("/")
        url = f"{issuer}/.well-known/openid-configuration"
        try:
            response = await self._client().get(url)
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcError("provider_unreachable", f"discovery failed: {exc}") from exc
        try:
            self._metadata = ProviderMetadata(
                issuer=str(document["issuer"]),
                authorization_endpoint=str(document["authorization_endpoint"]),
                token_endpoint=str(document["token_endpoint"]),
                jwks_uri=str(document["jwks_uri"]),
                end_session_endpoint=(
                    str(document["end_session_endpoint"])
                    if document.get("end_session_endpoint")
                    else None
                ),
            )
        except (KeyError, TypeError) as exc:
            raise OidcError("provider_unreachable", f"discovery incomplete: {exc}") from exc
        self._metadata_at = time.monotonic()
        return self._metadata

    async def jwks(self, *, force: bool = False) -> dict[str, Any]:
        """The provider's signing keys, cached."""
        if (
            not force
            and self._jwks is not None
            and time.monotonic() - self._jwks_at < METADATA_TTL_SECONDS
        ):
            return self._jwks
        metadata = await self.metadata()
        try:
            response = await self._client().get(metadata.jwks_uri)
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcError("provider_unreachable", f"JWKS fetch failed: {exc}") from exc
        if not isinstance(document, dict) or not isinstance(document.get("keys"), list):
            raise OidcError("provider_unreachable", "JWKS document has no keys")
        self._jwks = document
        self._jwks_at = time.monotonic()
        return document

    async def authorization_url(self, flow: FlowState, *, prompt: str | None = None) -> str:
        """Where to send the browser to start the flow.

        `prompt="none"` asks for a silent sign-in from the provider's own
        session; without one the provider answers `login_required`.
        """
        metadata = await self.metadata()
        challenge = _b64url(hashlib.sha256(flow.code_verifier.encode()).digest())
        params = {
            "response_type": "code",
            "client_id": self._settings.oidc_client_id,
            "redirect_uri": self._settings.oidc_redirect_uri,
            "scope": self._settings.oidc_scopes,
            "state": flow.state,
            "nonce": flow.nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if prompt:
            params["prompt"] = prompt
        separator = "&" if "?" in metadata.authorization_endpoint else "?"
        return f"{metadata.authorization_endpoint}{separator}{urlencode(params)}"

    async def logout_url(self, post_logout_redirect_uri: str) -> str | None:
        """The provider's end-session URL, or `None` when it has no such endpoint.

        No `id_token_hint`: the platform does not keep the ID token. The spec
        allows `client_id` in its place to identify the relying party.
        """
        metadata = await self.metadata()
        if not metadata.end_session_endpoint:
            return None
        params = {
            "client_id": self._settings.oidc_client_id,
            "post_logout_redirect_uri": post_logout_redirect_uri,
        }
        endpoint = metadata.end_session_endpoint
        separator = "&" if "?" in endpoint else "?"
        return f"{endpoint}{separator}{urlencode(params)}"

    async def exchange_code(self, code: str, flow: FlowState) -> Identity:
        """Redeem the authorization code and verify the ID token it returns."""
        metadata = await self.metadata()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._settings.oidc_redirect_uri,
            "client_id": self._settings.oidc_client_id,
            "code_verifier": flow.code_verifier,
        }
        if self._settings.oidc_client_secret:
            form["client_secret"] = self._settings.oidc_client_secret
        try:
            response = await self._client().post(
                metadata.token_endpoint, data=form, headers={"Accept": "application/json"}
            )
        except httpx.HTTPError as exc:
            raise OidcError("provider_unreachable", f"token request failed: {exc}") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise OidcError("token_exchange_failed", "token response is not JSON") from exc
        if response.status_code != 200 or not isinstance(body, dict):
            error = body.get("error") if isinstance(body, dict) else response.status_code
            raise OidcError("token_exchange_failed", f"token endpoint refused: {error}")
        id_token = body.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise OidcError("token_exchange_failed", "token response carries no id_token")
        claims = await self._verify_id_token(id_token, metadata, flow.nonce)
        return identity_from_claims(claims, groups_claim=self._settings.oidc_groups_claim)

    async def _verify_id_token(
        self, id_token: str, metadata: ProviderMetadata, nonce: str
    ) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise OidcError("invalid_id_token", f"unparseable id_token: {exc}") from exc
        keys = await self.jwks()
        kid = header.get("kid")
        if kid is not None and not any(k.get("kid") == kid for k in keys["keys"]):
            # A key the cache does not know: the provider may have rotated.
            keys = await self.jwks(force=True)
        candidates = _signing_keys(keys, kid)
        if not candidates:
            raise OidcError("invalid_id_token", "no provider key matches the id_token")
        # `at_hash` is not checked: the token endpoint response is trusted
        # transport for the access token, so it adds nothing here.
        error: jwt.PyJWTError | None = None
        for key in candidates:
            try:
                claims: dict[str, Any] = jwt.decode(
                    id_token,
                    key,
                    algorithms=ID_TOKEN_ALGORITHMS,
                    audience=self._settings.oidc_client_id,
                    issuer=metadata.issuer,
                    leeway=ID_TOKEN_LEEWAY_SECONDS,
                )
                break
            except jwt.InvalidSignatureError as exc:
                error = exc  # another key of the set may still verify it
            except jwt.PyJWTError as exc:
                raise OidcError("invalid_id_token", f"id_token rejected: {exc}") from exc
        else:
            raise OidcError("invalid_id_token", f"id_token rejected: {error}")
        if claims.get("nonce") != nonce:
            raise OidcError("invalid_id_token", "id_token nonce does not match the flow")
        return claims


def _signing_keys(jwks: dict[str, Any], kid: str | None) -> list[Any]:
    """The provider's signature keys a token with header `kid` may be signed with.

    The raw key objects, not `PyJWK`s: `jwt.decode` then applies the token's
    own `alg` (restricted to `ID_TOKEN_ALGORITHMS`), and a key whose type does
    not fit that algorithm is refused. Encryption keys and keys PyJWT cannot
    load are skipped; with no `kid` every signing key is a candidate.
    """
    try:
        key_set = jwt.PyJWKSet.from_dict(jwks)
    except jwt.PyJWTError:
        return []
    return [
        key.key
        for key in key_set.keys
        if key.public_key_use in (None, "sig") and (kid is None or key.key_id == kid)
    ]


def identity_from_claims(claims: dict[str, Any], *, groups_claim: str = "") -> Identity:
    """Pick subject, e-mail and name out of the ID token.

    Entra ID puts the address in ``preferred_username`` or ``upn`` when the
    ``email`` claim is not released; Keycloak, Cognito and Google use
    ``email``. The address is lower-cased because ``user.email`` is unique
    and the user table compares case-insensitively.
    """
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise OidcError("invalid_id_token", "id_token has no sub")
    email: str | None = None
    email_verified = False
    for claim in ("email", "preferred_username", "upn"):
        value = claims.get(claim)
        if isinstance(value, str) and "@" in value:
            email = value.strip().lower()
            # Cognito sends the flag as the string "true".
            verified = claims.get("email_verified")
            email_verified = claim == "email" and verified in (True, "true")
            break
    if email is None:
        raise OidcError("no_email", "id_token carries no e-mail address")
    name = claims.get("name")
    if not isinstance(name, str) or not name.strip():
        given = claims.get("given_name")
        family = claims.get("family_name")
        parts = [p for p in (given, family) if isinstance(p, str) and p.strip()]
        name = " ".join(parts) if parts else email.split("@", 1)[0]
    return Identity(
        subject=subject,
        email=email,
        display_name=name.strip()[:255],
        groups=_groups_from_claims(claims, groups_claim),
        email_verified=email_verified,
    )


def _groups_from_claims(claims: dict[str, Any], groups_claim: str) -> tuple[str, ...] | None:
    """The user's IdP groups, or ``None`` when sync must leave things as they are.

    ``None`` means "unknown": group sync is off (no claim configured) or the
    provider left the groups out because there are too many (Entra ID's
    overage: ``_claim_names`` names the claim, or ``hasgroups`` is true).
    A claim that is simply absent means *no groups*: Keycloak and Entra omit
    it for a user in none, and reading that as "unknown" would let someone
    removed from every group keep their access.
    """
    if not groups_claim:
        return None
    raw_groups = claims.get(groups_claim)
    if isinstance(raw_groups, list):
        return tuple(sorted({g for g in raw_groups if isinstance(g, str) and g}))
    if isinstance(raw_groups, str):
        # Some providers release a single group as a string.
        return (raw_groups,) if raw_groups else ()
    if raw_groups is not None:
        return None  # an unexpected shape: do not guess
    claim_names = claims.get("_claim_names")
    overage = isinstance(claim_names, dict) and groups_claim in claim_names
    if overage or claims.get("hasgroups") is True:
        return None
    return ()


# --- User resolution ---------------------------------------------------------


async def resolve_user(
    session: AsyncSession,
    identity: Identity,
    *,
    settings: Settings,
    client_ip: str | None,
    host: str | None = None,
) -> User:
    """Find or provision the user an ID token refers to. The caller commits.

    Match order: ``idp_subject`` (the stable link), then e-mail (links an
    account created locally or by an admin), then a new account if
    provisioning is on. Linking by e-mail needs ``email_verified`` from the
    provider, an account SCIM created, or ``APP_OIDC_TRUST_UNVERIFIED_EMAIL``: a provider
    that lets people type in any address would otherwise hand
    ``admin@acme.com``'s account to whoever registers that address there.
    """
    user = await session.scalar(select(User).where(User.idp_subject == identity.subject))
    if user is None:
        user = await session.scalar(select(User).where(User.email == identity.email))
        if user is not None:
            if user.idp_subject is not None and user.idp_subject != identity.subject:
                raise OidcError(
                    "account_conflict",
                    "the e-mail belongs to an account linked to another IdP subject",
                )
            # An account SCIM pushed came from the provider's own directory.
            trusted = (
                identity.email_verified
                or settings.oidc_trust_unverified_email
                or user.scim_external_id is not None
            )
            if not trusted:
                raise OidcError(
                    "email_unverified",
                    "the provider does not vouch for the e-mail of an existing account",
                )
            user.idp_subject = identity.subject
            audit.record(
                session,
                organization_id=user.organization_id,
                actor_id=user.id,
                action="user.link_idp",
                target_type="user",
                target_id=user.id,
                after={"idp_subject": identity.subject},
                ip=client_ip,
            )
    if user is None:
        if not settings.oidc_auto_provision:
            raise OidcError("not_provisioned", f"no account for {identity.email}")
        organization = await _provisioning_organization(session, settings)
        user = User(
            organization_id=organization.id,
            email=identity.email,
            display_name=identity.display_name,
            idp_subject=identity.subject,
            password_hash=None,
            is_active=True,
            is_superuser=False,
        )
        session.add(user)
        await session.flush()
        audit.record(
            session,
            organization_id=organization.id,
            actor_id=user.id,
            action="user.provision",
            target_type="user",
            target_id=user.id,
            after={"email": identity.email, "method": "oidc"},
            ip=client_ip,
        )
    if not user.is_active:
        audit.record(
            session,
            organization_id=user.organization_id,
            actor_id=None,
            action="auth.login_failed",
            target_type="user",
            target_id=user.id,
            after={"reason": "inactive", "method": "oidc"},
            ip=client_ip,
        )
        raise OidcError("inactive", f"account {user.id} is inactive")
    licence = await effective_license(session, settings, host=host, record=True)
    if not await seats.may_sign_in(session, user, licence):
        audit.record(
            session,
            organization_id=user.organization_id,
            actor_id=None,
            action="auth.login_failed",
            target_type="user",
            target_id=user.id,
            after={"reason": "seat_limit", "method": "oidc"},
            ip=client_ip,
        )
        raise OidcError("seat_limit", f"no free licence seat for account {user.id}")
    user.last_seen_at = datetime.now(UTC)
    audit.record(
        session,
        organization_id=user.organization_id,
        actor_id=user.id,
        action="auth.login",
        target_type="user",
        target_id=user.id,
        after={"method": "oidc"},
        ip=client_ip,
    )
    return user


async def _provisioning_organization(session: AsyncSession, settings: Settings) -> Organization:
    if settings.oidc_organization_slug:
        organization = await session.scalar(
            select(Organization).where(Organization.slug == settings.oidc_organization_slug)
        )
        if organization is None:
            raise OidcError(
                "no_organization",
                f"APP_OIDC_ORGANIZATION_SLUG={settings.oidc_organization_slug!r} does not exist",
            )
        return organization
    count = await session.scalar(select(func.count()).select_from(Organization))
    if count != 1:
        raise OidcError(
            "no_organization",
            f"{count} organisations exist; set APP_OIDC_ORGANIZATION_SLUG to pick one",
        )
    only = (await session.scalars(select(Organization))).first()
    assert only is not None  # count == 1 above
    return only


# --- Process-wide client -----------------------------------------------------

_client: OidcClient | None = None


def get_oidc_client() -> OidcClient:
    """The process's OIDC client. Tests override the dependency."""
    global _client  # one client per process, like the settings
    if _client is None or _client.settings is not get_settings():
        _client = OidcClient(get_settings())
    return _client


__all__ = [
    "FLOW_TTL",
    "FlowState",
    "Identity",
    "OidcClient",
    "OidcDisabledError",
    "OidcError",
    "ProviderMetadata",
    "decode_flow",
    "encode_flow",
    "get_oidc_client",
    "identity_from_claims",
    "new_flow",
    "resolve_user",
    "safe_next_path",
]
