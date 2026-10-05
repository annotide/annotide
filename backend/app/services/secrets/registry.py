"""Backend registry, TTL cache, and `resolve_secret`, the one function callers use.

`resolve_secret` is async because Key Vault, Secrets Manager and Secret
Manager are network calls. Values are cached by TTL so a vault is not called
per request; concurrent misses on one reference share a single lookup (a burst
of requests after the TTL expires would otherwise hit the vault once each and
can trip its throttling). A failure is never cached, and nothing here logs a
value.
"""

from __future__ import annotations

import asyncio
import time

import structlog

from app.services.secrets.backends import (
    AwsSecretsBackend,
    AzureKeyVaultBackend,
    EnvBackend,
    FileBackend,
    GcpSecretsBackend,
)
from app.services.secrets.base import SecretBackend, parse_ref
from app.services.secrets.errors import (
    SecretBackendError,
    SecretResolutionError,
    UnknownSecretSchemeError,
)

log = structlog.get_logger(__name__)

#: Seconds a resolved value is kept unless `configure` says otherwise.
DEFAULT_CACHE_TTL = 300

_registry: dict[str, SecretBackend] = {}
_cache: dict[str, tuple[str, float]] = {}
_locks: dict[str, asyncio.Lock] = {}
_default_ttl: int = DEFAULT_CACHE_TTL


def register(backend: SecretBackend) -> None:
    """Add or replace the backend for its scheme."""
    _registry[backend.scheme] = backend


def clear_cache() -> None:
    """Drop every cached value: tests, and after rotating a secret."""
    _cache.clear()
    _locks.clear()


def configure(
    *,
    file_root: str | None = None,
    azure_default_vault: str | None = None,
    azure_client_id: str | None = None,
    aws_default_region: str | None = None,
    cache_ttl: int = DEFAULT_CACHE_TTL,
) -> None:
    """(Re)register the standard backends with these settings; empties the cache.

    Called at import with the defaults and again at start-up with the
    settings (`APP_SECRET_*`), so `file_root` is in force before any request.
    """
    global _default_ttl
    _registry.clear()
    for backend in (
        EnvBackend(),
        FileBackend(allowed_root=file_root),
        AzureKeyVaultBackend(default_vault=azure_default_vault, client_id=azure_client_id),
        AwsSecretsBackend(default_region=aws_default_region),
        GcpSecretsBackend(),
    ):
        register(backend)
    _default_ttl = cache_ttl
    clear_cache()


async def aclose_backends() -> None:
    """Close the backends' clients (vault connections) on shutdown.

    The backends stay registered — with their settings — and reopen clients
    on the next lookup.
    """
    for backend in list(_registry.values()):
        await backend.aclose()


def _cached(secret_ref: str, now: float) -> str | None:
    hit = _cache.get(secret_ref)
    return hit[0] if hit is not None and hit[1] > now else None


async def _lookup(secret_ref: str) -> str:
    try:
        ref = parse_ref(secret_ref)
    except ValueError as exc:
        raise SecretResolutionError(secret_ref, str(exc)) from None
    backend = _registry.get(ref.scheme)
    if backend is None:
        raise UnknownSecretSchemeError(secret_ref, f"unknown secret store {ref.scheme!r}")
    try:
        value = await backend.get(ref)
    except SecretResolutionError:
        raise
    except SecretBackendError as exc:
        raise SecretResolutionError(secret_ref, str(exc)) from None
    except Exception as exc:
        # The store's own error text is not ours to trust not to echo values,
        # and chaining it would put it in the traceback; its type is enough.
        raise SecretResolutionError(
            secret_ref, f"{ref.scheme} lookup failed ({type(exc).__name__})"
        ) from None
    log.debug("secret.resolved", scheme=ref.scheme)
    return value


async def resolve_secret(
    secret_ref: str | None, *, use_cache: bool = True, cache_ttl: int | None = None
) -> str | None:
    """The value `secret_ref` points at; None only for no reference at all.

    ``None`` in is the normal case for a connector on a managed identity.
    Raises `SecretResolutionError` (or `UnknownSecretSchemeError`) when a
    reference is set but cannot be resolved — never None, which would read as
    "no credential needed" and try anonymous access. Values are cached for
    `cache_ttl` seconds (default from `configure`); `0` or `use_cache=False`
    bypasses the cache.
    """
    if secret_ref is None:
        return None
    ttl = _default_ttl if cache_ttl is None else cache_ttl
    if not use_cache or ttl <= 0:
        return await _lookup(secret_ref)
    hit = _cached(secret_ref, time.monotonic())
    if hit is not None:
        return hit
    lock = _locks.setdefault(secret_ref, asyncio.Lock())
    async with lock:
        # Whoever held the lock may have filled the cache meanwhile.
        hit = _cached(secret_ref, time.monotonic())
        if hit is not None:
            return hit
        value = await _lookup(secret_ref)
        _cache[secret_ref] = (value, time.monotonic() + ttl)
        return value
