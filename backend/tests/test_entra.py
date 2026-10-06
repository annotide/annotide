"""Entra sign-in to the backend's own PostgreSQL and Redis (SEC-1).

No Azure here: a fake credential hands out tokens, and the Redis connection's
socket side is a fake base class, so what is tested is the token handling —
which token, which user, and when a connection re-authenticates.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import jwt
import pytest
from azure.core.credentials import AccessToken

from app.core import redis as core_redis
from app.core.config import BackingServiceAuth, Settings, get_settings
from app.core.entra import (
    POSTGRES_SCOPE,
    REDIS_SCOPE,
    aclose_token_sources,
    set_credential,
    token_object_id,
    token_source,
)
from app.core.redis import (
    MIN_REAUTH_INTERVAL_SECONDS,
    REAUTH_MARGIN_SECONDS,
    EntraConnection,
    EntraSSLConnection,
    _EntraAuthMixin,
    arq_redis,
    redis_pool,
)
from app.db.session import ENTRA_POOL_RECYCLE_SECONDS, engine_options

OID = "11111111-2222-3333-4444-555555555555"


def _jwt(oid: str | None = OID, n: int = 0) -> str:
    claims: dict[str, Any] = {"n": n}
    if oid is not None:
        claims["oid"] = oid
    return jwt.encode(claims, "k" * 32, algorithm="HS256")


class FakeCredential:
    """A new token on every call, valid for `ttl` seconds."""

    def __init__(self, ttl: float = 3600) -> None:
        self.ttl = ttl
        self.calls: list[str] = []
        self.closed = False

    async def get_token(self, *scopes: str, **_: Any) -> AccessToken:
        self.calls.append(scopes[0])
        return AccessToken(_jwt(n=len(self.calls)), int(time.time() + self.ttl))

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def credential() -> Iterator[FakeCredential]:
    fake = FakeCredential()
    set_credential(fake)  # type: ignore[arg-type]  # duck-typed AsyncTokenCredential
    yield fake
    set_credential(None)


@pytest.fixture
def entra_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    monkeypatch.setenv("APP_DATABASE_AUTH", "entra")
    monkeypatch.setenv("APP_REDIS_AUTH", "entra")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


class TestTokens:
    async def test_password_is_the_token_for_the_scope(self, credential: FakeCredential) -> None:
        password = await token_source(POSTGRES_SCOPE).password()

        assert token_object_id(password) == OID
        assert credential.calls == [POSTGRES_SCOPE]

    def test_one_source_per_scope_on_one_credential(self, credential: FakeCredential) -> None:
        assert token_source(REDIS_SCOPE) is token_source(REDIS_SCOPE)
        assert token_source(REDIS_SCOPE) is not token_source(POSTGRES_SCOPE)

    def test_a_token_without_oid_is_refused(self) -> None:
        with pytest.raises(ValueError, match="oid"):
            token_object_id(_jwt(oid=None))

    async def test_shutdown_closes_the_credential(self, credential: FakeCredential) -> None:
        token_source(REDIS_SCOPE)
        await aclose_token_sources()

        assert credential.closed

    def test_the_real_credential_takes_the_user_assigned_client_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        class _Credential:
            def __init__(self, client_id: str | None = None) -> None:
                seen["client_id"] = client_id

        monkeypatch.setattr("azure.identity.aio.ManagedIdentityCredential", _Credential)
        monkeypatch.setenv("APP_MANAGED_IDENTITY_CLIENT_ID", "user-assigned-id")
        get_settings.cache_clear()
        set_credential(None)
        try:
            token_source(POSTGRES_SCOPE)
        finally:
            set_credential(None)
            get_settings.cache_clear()

        assert seen == {"client_id": "user-assigned-id"}


class TestDatabase:
    def test_password_auth_adds_nothing(self, settings: Settings) -> None:
        assert settings.database_auth is BackingServiceAuth.PASSWORD
        assert engine_options(settings) == {}

    async def test_entra_asks_for_a_token_per_connection_and_recycles(
        self, entra_settings: Settings, credential: FakeCredential
    ) -> None:
        options = engine_options(entra_settings)
        password = options["connect_args"]["password"]

        first, second = await password(), await password()

        assert first != second  # asked each time, not frozen at start-up
        assert credential.calls == [POSTGRES_SCOPE, POSTGRES_SCOPE]
        assert options["pool_recycle"] == ENTRA_POOL_RECYCLE_SECONDS < 3600


class TestRedisPool:
    def test_password_auth_is_plain_redis(self, settings: Settings) -> None:
        pool = redis_pool("redis://:secret@cache:6379/0")

        assert pool.connection_kwargs["password"] == "secret"
        assert "token_source" not in pool.connection_kwargs

    def test_entra_over_tls_keeps_tls(
        self, entra_settings: Settings, credential: FakeCredential
    ) -> None:
        pool = redis_pool("rediss://cache.redis.cache.windows.net:6380/0")

        assert pool.connection_class is EntraSSLConnection
        assert pool.connection_kwargs["token_source"] is token_source(REDIS_SCOPE)

    def test_entra_without_tls_and_extra_options(
        self, entra_settings: Settings, credential: FakeCredential
    ) -> None:
        pool = redis_pool("redis://cache:6379/0", socket_timeout=0.5)

        assert pool.connection_class is EntraConnection
        assert pool.connection_kwargs["socket_timeout"] == 0.5

    def test_a_password_in_the_url_is_dropped(
        self, entra_settings: Settings, credential: FakeCredential
    ) -> None:
        connection = redis_pool("rediss://:stale-key@cache:6380/0").make_connection()

        assert connection.password is None
        assert connection.credential_provider is not None

    def test_arq_client_owns_its_pool(
        self, entra_settings: Settings, credential: FakeCredential
    ) -> None:
        client = arq_redis("rediss://cache:6380/0")

        assert client.connection_pool.connection_class is EntraSSLConnection
        assert client.auto_close_connection_pool


class _FakeSocketSide:
    """Stands in for `redis.asyncio.Connection` below the mixin."""

    def __init__(self, credential_provider: Any, **_: Any) -> None:
        self.credential_provider = credential_provider
        self.connected = False
        self.sent: list[tuple[Any, ...]] = []
        self.reply: Any = b"OK"
        self.connects = 0
        self.disconnects = 0

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def on_connect(self) -> None:
        self.sent.append(("AUTH", *await self.credential_provider.get_credentials_async()))

    async def connect(self) -> None:
        if not self.connected:
            self.connected = True
            self.connects += 1
            await self.on_connect()

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnects += 1

    async def send_command(self, *args: Any, **_: Any) -> None:
        self.sent.append(args)

    async def read_response(self) -> Any:
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


class _Conn(_EntraAuthMixin, _FakeSocketSide):
    pass


class TestRedisReauth:
    async def test_signs_in_as_the_identity_with_the_token(
        self, credential: FakeCredential
    ) -> None:
        conn = _Conn(token_source=token_source(REDIS_SCOPE))
        await conn.connect()

        command, user, password = conn.sent[0]
        assert (command, user) == ("AUTH", OID)
        assert token_object_id(password) == OID

    async def test_a_user_name_from_the_url_wins(self, credential: FakeCredential) -> None:
        conn = _Conn(token_source=token_source(REDIS_SCOPE), username="alias", password="x")
        await conn.connect()

        assert conn.sent[0][1] == "alias"

    async def test_no_reauth_while_the_token_is_fresh(self, credential: FakeCredential) -> None:
        conn = _Conn(token_source=token_source(REDIS_SCOPE))
        await conn.connect()
        await conn.connect()

        assert len(conn.sent) == 1
        assert conn._entra_reauth_at == pytest.approx(
            time.time() + 3600 - REAUTH_MARGIN_SECONDS, abs=5
        )

    async def test_reauthenticates_in_place_before_expiry(
        self, credential: FakeCredential, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _Conn(token_source=token_source(REDIS_SCOPE))
        await conn.connect()
        first_token = conn.sent[0][2]

        later = time.time() + 3600 - REAUTH_MARGIN_SECONDS + 1
        monkeypatch.setattr(core_redis.time, "time", lambda: later)
        await conn.connect()

        assert conn.connects == 1  # same socket
        assert conn.sent[1][:2] == ("AUTH", OID)
        assert conn.sent[1][2] != first_token
        assert conn._entra_reauth_at > later

    async def test_a_rejected_reauth_reconnects(
        self, credential: FakeCredential, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from redis.exceptions import ResponseError

        conn = _Conn(token_source=token_source(REDIS_SCOPE))
        await conn.connect()
        conn.reply = ResponseError("WRONGPASS")
        monkeypatch.setattr(core_redis.time, "time", lambda: 1e12)

        await conn.connect()

        assert (conn.disconnects, conn.connects) == (1, 2)
        assert conn.is_connected

    async def test_no_token_is_a_redis_connection_error(self) -> None:
        """Callers that fail open on a down Redis (rate limit) must see a RedisError."""
        from redis.exceptions import ConnectionError as RedisConnectionError

        class _Down(FakeCredential):
            async def get_token(self, *scopes: str, **_: Any) -> AccessToken:
                raise RuntimeError("identity endpoint unreachable")

        set_credential(_Down())  # type: ignore[arg-type]
        try:
            conn = _Conn(token_source=token_source(REDIS_SCOPE))
            with pytest.raises(RedisConnectionError, match="RuntimeError"):
                await conn.connect()
        finally:
            set_credential(None)

    async def test_a_nearly_expired_token_does_not_reauth_on_every_command(self) -> None:
        set_credential(FakeCredential(ttl=10))  # type: ignore[arg-type]
        try:
            conn = _Conn(token_source=token_source(REDIS_SCOPE))
            await conn.connect()
            await conn.connect()
        finally:
            set_credential(None)

        assert len(conn.sent) == 1
        assert conn._entra_reauth_at >= time.time() + MIN_REAUTH_INTERVAL_SECONDS - 1


class TestEntryPoints:
    async def test_queue_reports_an_unreachable_redis(
        self, entra_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services import queue

        class _Down:
            closed = False

            async def ping(self) -> None:
                raise OSError("unreachable")

            async def aclose(self) -> None:
                _Down.closed = True

        monkeypatch.setattr(queue, "arq_redis", lambda url: _Down())

        with pytest.raises(queue.QueueUnavailableError):
            await queue.create_queue("rediss://cache:6380/0")
        assert _Down.closed

    async def test_queue_uses_the_entra_pool(
        self, entra_settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.services import queue

        class _Up:
            async def ping(self) -> bool:
                return True

        up = _Up()
        monkeypatch.setattr(queue, "arq_redis", lambda url: up)

        created = await queue.create_queue("rediss://cache:6380/0")

        assert isinstance(created, queue.ArqJobQueue)
        assert created._pool is up  # type: ignore[comparison-overlap]

    @pytest.mark.parametrize(("value", "code"), [(b"2026-10-01 j_complete=3", 0), (None, 1)])
    async def test_worker_check(
        self, monkeypatch: pytest.MonkeyPatch, value: bytes | None, code: int
    ) -> None:
        from app.worker import check

        keys: list[str] = []

        class _Redis:
            async def get(self, key: str) -> bytes | None:
                keys.append(key)
                return value

            async def aclose(self) -> None:
                pass

        monkeypatch.setattr(check, "arq_redis", lambda **_: _Redis())

        assert await check.check() == code
        assert keys == ["arq:queue:health-check"]
