"""SAML 2.0 single sign-on (AUTH-1), beside OIDC.

Service-provider-initiated Web Browser SSO against any SAML 2.0 identity
provider (Entra ID, Okta, Google Workspace, ADFS, Keycloak, Shibboleth),
configured with ``APP_SAML_*`` (see ``core/config.py``). pysaml2 does the
protocol and checks signatures with the ``xmlsec1`` binary.

The flow, from the browser's point of view:

1. ``GET /auth/saml/login`` — the API builds an AuthnRequest, seals its id
   and the path to return to into a short-lived signed cookie, and redirects
   to the IdP (HTTP-Redirect binding).
2. The IdP authenticates the user and the browser POSTs a ``SAMLResponse`` to
   ``/auth/saml/acs`` (HTTP-POST binding).
3. The API checks the response against the IdP's metadata: the signature on
   the assertion or the response, issuer, audience, destination, validity
   window, and that it answers the request id in this browser's cookie. An
   unsolicited (IdP-initiated) response is refused: nothing would tie it to
   the browser that receives it, which is a login-CSRF.
4. The NameID and attributes become an :class:`Identity`, and from there the
   OIDC path is reused: ``resolve_user``, group sync, the platform's own
   token in a URL fragment.

Not supported: IdP-initiated sign-in, encrypted assertions (TLS protects the
transport; turn assertion encryption off at the IdP), single logout, and
transient NameIDs, which change on every sign-in and so identify no one.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol

import httpx
import jwt
import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from saml2 import BINDING_HTTP_POST, BINDING_HTTP_REDIRECT
from saml2.client import Saml2Client
from saml2.config import SPConfig
from saml2.metadata import create_metadata_string
from saml2.response import StatusError, UnsolicitedResponse
from saml2.saml import NAMEID_FORMAT_EMAILADDRESS, NAMEID_FORMAT_TRANSIENT
from saml2.sigver import SigverError, get_xmlsec_binary

from app.core.config import Settings, get_settings
from app.core.redis import redis_client
from app.core.security import ALGORITHM, OWN_TOKEN_OPTIONS
from app.services.oidc import Identity, OidcError, SignInPolicy, safe_next_path

log = structlog.get_logger(__name__)

#: How long the browser has to complete the round trip through the IdP.
FLOW_TTL: Final = timedelta(minutes=10)
#: IdP metadata from a URL is re-read after this many seconds.
METADATA_TTL_SECONDS: Final = 3600
#: After a failed refresh, the last good metadata serves this long before
#: the next attempt, so an IdP outage does not make every sign-in wait.
METADATA_RETRY_SECONDS: Final = 300
#: Clock skew tolerated on the assertion's validity window.
CLOCK_SKEW_SECONDS: Final = 60
#: A SAMLResponse larger than this is refused before any XML is parsed.
MAX_RESPONSE_BYTES: Final = 256 * 1024
#: Prefix of SAML subjects in ``user.idp_subject``, so an OIDC ``sub`` and a
#: SAML NameID with the same text can never be the same user.
SUBJECT_PREFIX: Final = "saml:"
_SUBJECT_MAX: Final = 255
_FLOW_TOKEN_TYPE: Final = "saml_flow"


class SamlError(OidcError):
    """A SAML sign-in failed; ``code`` goes to the login page as ``?error=``."""


@dataclass(frozen=True, slots=True)
class SamlFlow:
    """What the login step remembers until the response arrives."""

    request_id: str
    next_path: str


class UsedRequests(Protocol):
    """Request ids whose response has been accepted: a bearer assertion is single-use."""

    async def claim(self, request_id: str, ttl_seconds: int) -> bool:
        """True the first time, False for a replay."""
        ...


class RedisUsedRequests:
    """`SET NX EX` in the shared Redis. Fails open, like the rate limiter: a
    replay also needs the victim's HttpOnly flow cookie, and refusing every
    SAML sign-in during a Redis blip would be the larger harm."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def claim(self, request_id: str, ttl_seconds: int) -> bool:
        try:
            first = await self._redis.set(f"saml:used:{request_id}", "1", nx=True, ex=ttl_seconds)
        except RedisError as exc:
            log.warning("saml.replay_check_unavailable", error=str(exc))
            return True
        return bool(first)


class MemoryUsedRequests:
    """For tests and single-process development."""

    def __init__(self) -> None:
        self._until: dict[str, float] = {}

    async def claim(self, request_id: str, ttl_seconds: int) -> bool:
        now = time.monotonic()
        self._until = {key: until for key, until in self._until.items() if until > now}
        if request_id in self._until:
            return False
        self._until[request_id] = now + ttl_seconds
        return True


def saml_policy(settings: Settings) -> SignInPolicy:
    return SignInPolicy(
        method="saml",
        auto_provision=settings.saml_auto_provision,
        trust_email=settings.saml_trust_email,
        organization_slug=settings.saml_organization_slug,
        organization_setting="APP_SAML_ORGANIZATION_SLUG",
    )


# --- Flow cookie -------------------------------------------------------------


def encode_flow(flow: SamlFlow, settings: Settings) -> str:
    """Seal the request id into a signed, expiring token for the cookie."""
    if not settings.secret_key:
        raise RuntimeError("APP_SECRET_KEY must be set to start a SAML flow")
    now = datetime.now(UTC)
    claims = {
        "typ": _FLOW_TOKEN_TYPE,
        "iat": now,
        "exp": now + FLOW_TTL,
        "rid": flow.request_id,
        "next": flow.next_path,
    }
    encoded: str = jwt.encode(claims, settings.secret_key, algorithm=ALGORITHM)
    return encoded


def decode_flow(value: str, settings: Settings) -> SamlFlow:
    """Reverse :func:`encode_flow`. Raises ``SamlError`` on anything off."""
    if not settings.secret_key:
        raise RuntimeError("APP_SECRET_KEY must be set to complete a SAML flow")
    keys = [settings.secret_key]
    if settings.secret_key_previous and settings.secret_key_previous != settings.secret_key:
        keys.append(settings.secret_key_previous)  # a rotation mid-flow
    for key in keys:
        try:
            claims = jwt.decode(value, key, algorithms=[ALGORITHM], options=OWN_TOKEN_OPTIONS)
        except jwt.InvalidSignatureError:
            continue
        except jwt.PyJWTError as exc:
            raise SamlError("flow_expired", f"flow cookie rejected: {exc}") from exc
        if claims.get("typ") != _FLOW_TOKEN_TYPE or "rid" not in claims:
            raise SamlError("flow_expired", "flow cookie has the wrong type")
        return SamlFlow(request_id=str(claims["rid"]), next_path=str(claims.get("next", "/")))
    raise SamlError("flow_expired", "flow cookie signature does not verify")


# --- Attributes -> Identity ----------------------------------------------------


def _names(setting: str) -> list[str]:
    return [name.strip().lower() for name in setting.split(",") if name.strip()]


def _attribute(attributes: dict[str, list[Any]], wanted: list[str]) -> list[str] | None:
    """The first wanted attribute present: by exact name or a URI name's last segment."""
    lowered = {key.lower(): values for key, values in attributes.items()}
    for name in wanted:
        for key, values in lowered.items():
            if key == name or key.endswith(("/" + name, ":" + name)):
                return [str(value) for value in values if value is not None and str(value)]
    return None


def identity_from_response(
    name_id: str | None,
    name_id_format: str | None,
    attributes: dict[str, list[Any]],
    settings: Settings,
) -> Identity:
    """Map a verified assertion onto the fields ``resolve_user`` needs."""
    if not name_id:
        raise SamlError("invalid_response", "the assertion has no NameID")
    if name_id_format == NAMEID_FORMAT_TRANSIENT:
        raise SamlError(
            "unsupported_name_id",
            "a transient NameID changes on every sign-in; configure persistent or e-mail",
        )
    subject = SUBJECT_PREFIX + name_id
    if len(subject) > _SUBJECT_MAX:
        subject = SUBJECT_PREFIX + "sha256:" + hashlib.sha256(name_id.encode()).hexdigest()

    emails = _attribute(attributes, _names(settings.saml_email_attribute))
    email = emails[0] if emails else None
    if email is None and name_id_format == NAMEID_FORMAT_EMAILADDRESS:
        email = name_id
    if not email or "@" not in email:
        raise SamlError("no_email", "the assertion carries no e-mail address")
    email = email.strip().lower()

    names = _attribute(attributes, _names(settings.saml_name_attribute))
    if names:
        display_name = names[0]
    else:
        given = _attribute(attributes, ["givenname", "firstname"]) or []
        family = _attribute(attributes, ["surname", "sn", "lastname"]) or []
        display_name = " ".join([*given[:1], *family[:1]]) or email.split("@")[0]

    groups: tuple[str, ...] | None = None
    if settings.saml_groups_attribute:
        found = _attribute(attributes, _names(settings.saml_groups_attribute))
        # Absent attribute: leave memberships alone, as OIDC does (AUTH-3).
        groups = tuple(found) if found is not None else None
    # SAML has no email_verified; SignInPolicy.trust_email decides linking.
    return Identity(subject=subject, email=email, display_name=display_name[:255], groups=groups)


# --- Service provider ----------------------------------------------------------


class SamlClient:
    """The service provider. One instance per process."""

    def __init__(
        self,
        settings: Settings,
        *,
        http: httpx.AsyncClient | None = None,
        used: UsedRequests | None = None,
    ) -> None:
        self.settings = settings
        #: For the metadata URL; tests pass one with a mock transport.
        self._http = http
        self._used = used
        self._metadata: str | None = None
        self._metadata_at = 0.0
        self._config: SPConfig | None = None
        self._config_for: str | None = None
        self._lock = asyncio.Lock()

    def _base_config(self, idp_metadata: str | None) -> dict[str, Any]:
        try:
            xmlsec = get_xmlsec_binary()
        except SigverError as exc:
            raise SamlError("not_configured", "xmlsec1 is not installed") from exc
        return {
            "entityid": self.settings.saml_entity_id,
            "xmlsec_binary": xmlsec,
            "accepted_time_diff": CLOCK_SKEW_SECONDS,
            # Entra and Okta send attributes named by URI, which pysaml2 has no
            # converter for; keep them under their own names.
            "allow_unknown_attributes": True,
            "service": {
                "sp": {
                    "name": "Annotide",
                    "endpoints": {
                        "assertion_consumer_service": [
                            (self.settings.saml_acs_url, BINDING_HTTP_POST)
                        ],
                    },
                    "allow_unsolicited": False,
                    "authn_requests_signed": False,
                    "want_response_signed": False,
                    "want_assertions_signed": False,
                    "want_assertions_or_response_signed": True,
                },
            },
            "metadata": {"inline": [idp_metadata]} if idp_metadata else {},
        }

    async def _idp_metadata(self) -> str:
        fresh = time.monotonic() - self._metadata_at < METADATA_TTL_SECONDS
        if self._metadata is not None and fresh:
            return self._metadata
        try:
            if self.settings.saml_idp_metadata_file:
                path = Path(self.settings.saml_idp_metadata_file)
                text = await asyncio.to_thread(path.read_text, encoding="utf-8")
            else:
                assert self.settings.saml_idp_metadata_url is not None
                text = await self._fetch(self.settings.saml_idp_metadata_url)
        except (OSError, httpx.HTTPError, SamlError) as exc:
            if self._metadata is not None:
                # Keep signing people in on the last good metadata, and try
                # again in a few minutes, not on every request.
                log.warning("saml.metadata_refresh_failed", error=str(exc))
                self._metadata_at = time.monotonic() - METADATA_TTL_SECONDS + METADATA_RETRY_SECONDS
                return self._metadata
            if isinstance(exc, SamlError):
                raise
            raise SamlError("provider_unreachable", f"IdP metadata: {exc}") from exc
        self._metadata, self._metadata_at = text, time.monotonic()
        return text

    async def _fetch(self, url: str) -> str:
        if self._http is not None:
            response = await self._http.get(url)
        else:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                response = await client.get(url)
        response.raise_for_status()
        # The metadata is unsigned and carries the IdP's signing key: TLS is
        # its only protection, so a redirect must not step down to http.
        if self.settings.env == "production" and response.url.scheme != "https":
            raise SamlError("provider_unreachable", f"IdP metadata came over {response.url.scheme}")
        return response.text

    async def _sp_config(self) -> tuple[SPConfig, str]:
        """The SP configuration with the IdP's current metadata, and the IdP's entity id."""
        async with self._lock:
            metadata = await self._idp_metadata()
            if self._config is None or self._config_for != metadata:
                config = SPConfig()
                try:
                    await asyncio.to_thread(config.load, self._base_config(metadata))
                except SamlError:
                    raise
                except Exception as exc:  # pysaml2 raises many types for bad metadata
                    raise SamlError("provider_unreachable", f"IdP metadata: {exc}") from exc
                self._config, self._config_for = config, metadata
            config = self._config
        idps = list(config.metadata.identity_providers())
        wanted = self.settings.saml_idp_entity_id
        if wanted:
            if wanted not in idps:
                raise SamlError("no_idp", f"{wanted} is not in the IdP metadata")
            return config, wanted
        if len(idps) != 1:
            raise SamlError(
                "no_idp", f"the metadata names {len(idps)} IdPs; set APP_SAML_IDP_ENTITY_ID"
            )
        return config, idps[0]

    async def login_url(self, next_path: str | None) -> tuple[str, SamlFlow]:
        """Where to send the browser, and the flow to seal into its cookie."""
        config, idp = await self._sp_config()
        client = Saml2Client(config=config)
        try:
            request_id, info = client.prepare_for_authenticate(
                entityid=idp, binding=BINDING_HTTP_REDIRECT
            )
            location = dict(info["headers"])["Location"]
        except Exception as exc:  # e.g. metadata with no HTTP-Redirect SSO endpoint
            raise SamlError("no_idp", f"cannot build an AuthnRequest: {exc}") from exc
        return location, SamlFlow(request_id=request_id, next_path=safe_next_path(next_path))

    async def identity(self, saml_response: str, flow: SamlFlow) -> Identity:
        """Verify a posted ``SAMLResponse`` against the flow and return who signed in."""
        if len(saml_response) > MAX_RESPONSE_BYTES:
            raise SamlError("invalid_response", "SAMLResponse is too large")
        try:
            base64.b64decode(saml_response, validate=True)
        except ValueError as exc:
            raise SamlError("invalid_response", "SAMLResponse is not base64") from exc
        config, idp = await self._sp_config()
        # A fresh client per response: Saml2Client caches every identity it
        # has seen in memory, which would grow without bound in a server.
        client = Saml2Client(config=config)
        try:
            response = await asyncio.to_thread(
                client.parse_authn_request_response,
                saml_response,
                BINDING_HTTP_POST,
                {flow.request_id: flow.next_path},
            )
        except StatusError as exc:
            raise SamlError("provider_denied", f"IdP status: {exc}") from exc
        except UnsolicitedResponse as exc:
            raise SamlError("state_mismatch", str(exc)) from exc
        except Exception as exc:  # signature, audience, time, XML: all mean "do not trust it"
            raise SamlError("invalid_response", f"{type(exc).__name__}: {str(exc)[:300]}") from exc
        if response is None:
            raise SamlError("invalid_response", "no response")
        self._check_addressed_to_us(response, idp)
        used = self._used or get_used_requests()
        if not await used.claim(flow.request_id, int(FLOW_TTL.total_seconds())):
            raise SamlError("flow_expired", f"request {flow.request_id} was already answered")
        name_id = response.name_id
        return identity_from_response(
            name_id.text if name_id is not None else None,
            name_id.format if name_id is not None else None,
            response.ava or {},
            self.settings,
        )

    def _check_addressed_to_us(self, response: Any, idp: str) -> None:
        """What pysaml2 leaves optional, required here.

        pysaml2 accepts an assertion without an AudienceRestriction, skips the
        bearer ``Recipient`` unless given connection info, and compares only
        the (possibly unsigned) Response's issuer. The Web Browser SSO profile
        requires all of them, and every mainstream IdP sends them.
        """
        assertion = response.assertion
        issuer = assertion.issuer.text if assertion is not None and assertion.issuer else None
        if response.issuer() != idp or issuer != idp:
            raise SamlError("invalid_response", "the assertion is not from the configured IdP")
        entity_id, acs = self.settings.saml_entity_id, self.settings.saml_acs_url
        conditions = assertion.conditions
        audiences = {
            audience.text
            for restriction in (conditions.audience_restriction if conditions else []) or []
            for audience in restriction.audience or []
        }
        if entity_id not in audiences:
            raise SamlError("invalid_response", f"assertion audience {sorted(audiences)}")
        recipients = {
            confirmation.subject_confirmation_data.recipient
            for confirmation in (assertion.subject.subject_confirmation or [])
            if confirmation.subject_confirmation_data is not None
        }
        if acs not in recipients:
            raise SamlError("invalid_response", f"bearer recipient {sorted(map(str, recipients))}")
        destination = response.response.destination
        if destination is not None and destination != acs:
            raise SamlError("invalid_response", f"response destination {destination}")

    async def sp_metadata(self) -> str:
        """This service provider's metadata, for registering it with the IdP."""
        config = SPConfig()
        try:
            await asyncio.to_thread(config.load, self._base_config(None))
            metadata: bytes = create_metadata_string(None, config=config, sign=False)
        except SamlError:
            raise
        except Exception as exc:  # pysaml2 raises many types for a bad configuration
            raise SamlError("not_configured", f"SP metadata: {exc}") from exc
        return metadata.decode()


_used: UsedRequests | None = None


def get_used_requests() -> UsedRequests:
    """The process's replay guard, in the shared Redis."""
    global _used  # one per process, like the rate limiter
    if _used is None:
        _used = RedisUsedRequests(redis_client(socket_connect_timeout=0.5, socket_timeout=0.5))
    return _used


_client: SamlClient | None = None


def get_saml_client() -> SamlClient:
    """The process's SAML client. Tests override the dependency."""
    global _client  # one client per process, like the settings
    if _client is None or _client.settings is not get_settings():
        _client = SamlClient(get_settings())
    return _client


__all__ = [
    "FLOW_TTL",
    "MemoryUsedRequests",
    "SamlClient",
    "SamlError",
    "SamlFlow",
    "decode_flow",
    "encode_flow",
    "get_saml_client",
    "identity_from_response",
    "saml_policy",
]
