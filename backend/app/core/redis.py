"""Redis clients built from settings: the one place that knows `APP_REDIS_AUTH`.

With `password` this is plain `redis.asyncio` from `APP_REDIS_URL`. With
`entra` every connection signs in with a token from the managed identity
(`app.core.entra`), user name the identity's object id, and stays signed in:
before a pooled connection is handed out, one whose token is about to expire
re-sends `AUTH` with a fresh token on the same socket. Azure closes a
connection whose token expired, so without this a long-lived pool would fail
an hour (or a day) after start-up. If the re-`AUTH` fails the connection is
dropped and reopened, which authenticates afresh.

Commands are short (arq polls, it does not block), so checking at hand-out is
enough: a connection is never in use across the margin below.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog
from arq.connections import ArqRedis
from redis.asyncio import ConnectionPool, Redis
from redis.asyncio.connection import Connection, SSLConnection, parse_url
from redis.credentials import CredentialProvider
from redis.exceptions import AuthenticationError, RedisError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.utils import str_if_bytes

from app.core.config import BackingServiceAuth, get_settings
from app.core.entra import REDIS_SCOPE, TokenSource, token_object_id, token_source

if TYPE_CHECKING:
    from azure.core.credentials import AccessToken

log = structlog.get_logger(__name__)

#: Re-authenticate a connection this long before its token expires. Below
#: `azure-identity`'s own five-minute refresh, so the token handed back for
#: the re-`AUTH` is always a new one.
REAUTH_MARGIN_SECONDS = 120
#: ...but never more often than this, should the identity endpoint be down and
#: keep returning the old, nearly expired token.
MIN_REAUTH_INTERVAL_SECONDS = 30


def _reauth_at(token: AccessToken) -> float:
    return max(token.expires_on - REAUTH_MARGIN_SECONDS, time.time() + MIN_REAUTH_INTERVAL_SECONDS)


class EntraCredentialProvider(CredentialProvider):
    """`(user, token)` for a new connection; the user defaults to the token's `oid`."""

    def __init__(self, tokens: TokenSource, username: str | None = None) -> None:
        self._tokens = tokens
        self._username = username

    async def credentials(self) -> tuple[str, str, AccessToken]:
        try:
            token = await self._tokens.get()
            return self._username or token_object_id(token.token), token.token, token
        except Exception as exc:
            # As a Redis error, so callers that fail open or answer 503 on an
            # unreachable Redis (rate limit, queue) do the same here.
            raise RedisConnectionError(f"no Entra token for Redis: {type(exc).__name__}") from exc

    async def get_credentials_async(self) -> tuple[str, str]:
        username, password, _ = await self.credentials()
        return username, password

    def get_credentials(self) -> tuple[str, str]:  # pragma: no cover - async client only
        raise NotImplementedError("Entra credentials are async-only")


class _EntraAuthMixin:
    """Signs a connection in with a token and re-signs it before the token expires.

    Mixed into `Connection` and `SSLConnection`; `token_source` arrives as a
    connection keyword from the pool. A user name or password in the URL is
    dropped: the user is the identity, the password is the token.
    """

    def __init__(self, *args: Any, token_source: TokenSource, **kwargs: Any) -> None:
        username = kwargs.pop("username", None)
        kwargs.pop("password", None)
        self._entra = EntraCredentialProvider(token_source, username)
        self._entra_reauth_at = 0.0
        super().__init__(*args, credential_provider=self._entra, **kwargs)  # type: ignore[call-arg]  # mixin over Connection

    async def on_connect(self) -> None:
        # The token `on_connect` signs in with comes from the same cache, so
        # it is this one or a newer one: timing the re-AUTH off this token is
        # never late.
        _, _, token = await self._entra.credentials()
        await super().on_connect()  # type: ignore[misc]  # mixin over Connection
        self._entra_reauth_at = _reauth_at(token)

    async def connect(self) -> None:
        conn: Any = self
        if conn.is_connected and time.time() >= self._entra_reauth_at:
            await self._reauthenticate()
        await super().connect()  # type: ignore[misc]  # mixin over Connection

    async def _reauthenticate(self) -> None:
        conn: Any = self
        try:
            username, password, token = await self._entra.credentials()
            await conn.send_command("AUTH", username, password, check_health=False)
            if str_if_bytes(await conn.read_response()) != "OK":
                raise AuthenticationError("AUTH was not accepted")
        except (RedisError, OSError) as exc:
            # A fresh connection signs in from scratch; `connect()` opens it.
            log.warning("redis.reauth_failed", error=type(exc).__name__)
            await conn.disconnect()
            return
        self._entra_reauth_at = _reauth_at(token)
        log.debug("redis.reauthenticated")


class EntraConnection(_EntraAuthMixin, Connection):
    """`redis://` with Entra sign-in."""


class EntraSSLConnection(_EntraAuthMixin, SSLConnection):
    """`rediss://` with Entra sign-in (Azure: always this, port 6380)."""


def redis_pool(url: str | None = None, **kwargs: Any) -> ConnectionPool:
    """A connection pool for `url` (default `APP_REDIS_URL`), signed in per `APP_REDIS_AUTH`."""
    settings = get_settings()
    url = url or settings.redis_url
    if settings.redis_auth is not BackingServiceAuth.ENTRA:
        return ConnectionPool.from_url(url, **kwargs)
    # Not `from_url`: it lets the URL's scheme pick the connection class.
    options: dict[str, Any] = {**kwargs, **parse_url(url)}
    tls = options.get("connection_class") is SSLConnection
    options["connection_class"] = EntraSSLConnection if tls else EntraConnection
    options["token_source"] = token_source(REDIS_SCOPE)
    return ConnectionPool(**options)


def redis_client(url: str | None = None, **kwargs: Any) -> Redis:
    """A client on its own pool; closing the client closes the pool."""
    return Redis.from_pool(redis_pool(url, **kwargs))


def arq_redis(url: str | None = None, **kwargs: Any) -> ArqRedis:
    """An arq client on its own pool, for when arq cannot build one from a DSN.

    arq's `create_pool` only knows a static password, so Entra sign-in goes
    through this instead (`app.services.queue`, the worker).
    """
    redis = ArqRedis(redis_pool(url, **kwargs))
    # As `Redis.from_pool`: the client owns the pool and closes it.
    redis.auto_close_connection_pool = True
    return redis
