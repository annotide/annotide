"""Open a configured `connector` row as a live storage connector.

The row stores a *reference* to its credential (`secret_ref`), never the
credential itself. Every place that needs to talk to storage has to resolve
that reference and build the connector — and then close it, or the SDK's
connection pool leaks. Doing it in one place keeps that sequence right.

`storage_for` leases instances from a process-wide pool instead of building
one per call: an instance carries the SDK's connection pool, its credential's
token cache and (Azure, managed identity) the user delegation key, all of which
a per-request instance threw away — one extra network round trip per signed
URL. An instance is rebuilt when the row's config or the resolved secret
changes, and closed once the last lease on a replaced or evicted one ends.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.errors import NotFoundError, ValidationFailedError
from app.connectors.base import StorageConnector
from app.connectors.registry import build_connector
from app.core.config import get_settings
from app.models import Connector, ConnectorType
from app.services.secrets import resolve_secret

#: Types that refuse writes (SRC-1): a source, never a project's result connector.
READ_ONLY_CONNECTOR_TYPES = frozenset({ConnectorType.HTTP})


def _builder_config(connector: Connector) -> dict[str, Any]:
    config = dict(connector.config)
    # The row's own id, so a connector that proxies through this API (`local`)
    # can address and sign its media route. Kept out of the JSONB column: it is
    # the row's identity, not part of its configuration.
    config.setdefault("connector_id", str(connector.id))
    # The row's `identity_type` column is authoritative; builders read it from
    # the config dict (an older row may also carry it there).
    config.setdefault("identity_type", connector.identity_type.value)
    # Where a model service reaches the API, for `internal` signed URLs of the
    # connectors that proxy media through it. A deployment fact, not a row's.
    internal_api_url = get_settings().internal_api_url
    if internal_api_url:
        config.setdefault("internal_base_url", internal_api_url)
    return config


async def open_storage(connector: Connector) -> StorageConnector:
    """Resolve the connector's secret and build it. The caller closes it.

    Raises :class:`~app.connectors.errors.ConnectorError` for a bad config and
    :class:`~app.services.secrets.SecretResolutionError` when the credential
    reference cannot be resolved. Neither is swallowed: a job that cannot reach
    storage should fail loudly, not run against nothing.
    """
    secret = await resolve_secret(connector.secret_ref)
    return build_connector(connector.type.value, _builder_config(connector), secret)


@dataclass(eq=False)
class _Pooled:
    storage: StorageConnector
    fingerprint: str
    loop: asyncio.AbstractEventLoop
    leases: int = 0
    retired: bool = False
    last_used: float = field(default_factory=time.monotonic)


class ConnectorPool:
    """Live connector instances shared by id, leased by `storage_for`.

    Only instances created on the running event loop are reused: SDK clients
    are bound to the loop that opened them. At most ``max_size`` are kept; the
    least recently used idle ones go first. Never shares a secret beyond the
    instance that already holds it: the fingerprint stores a hash only.
    """

    def __init__(self, max_size: int = 64) -> None:
        self._max_size = max_size
        self._entries: dict[UUID, _Pooled] = {}

    def __len__(self) -> int:
        return len(self._entries)

    @asynccontextmanager
    async def lease(self, connector: Connector) -> AsyncIterator[StorageConnector]:
        secret = await resolve_secret(connector.secret_ref)
        config = _builder_config(connector)
        fingerprint = hashlib.sha256(
            json.dumps([connector.type.value, config, secret], sort_keys=True, default=str).encode()
        ).hexdigest()
        loop = asyncio.get_running_loop()

        # No await between the lookup and the insert, so two concurrent leases
        # of the same connector cannot both build it.
        entry = self._entries.get(connector.id)
        stale = None
        if entry is None or entry.fingerprint != fingerprint or entry.loop is not loop:
            stale = entry
            entry = _Pooled(
                build_connector(connector.type.value, config, secret), fingerprint, loop
            )
            self._entries[connector.id] = entry
        entry.leases += 1
        if stale is not None:
            await self._retire(connector.id, stale)
        await self._evict()

        try:
            yield entry.storage
        finally:
            entry.leases -= 1
            entry.last_used = time.monotonic()
            if entry.retired and entry.leases == 0:
                await self._close(entry)

    def discard(self, connector_id: UUID) -> None:
        """Forget a connector (deleted or edited); closed when its last lease ends."""
        entry = self._entries.pop(connector_id, None)
        if entry is not None:
            entry.retired = True

    async def _retire(self, connector_id: UUID, entry: _Pooled) -> None:
        if self._entries.get(connector_id) is entry:
            del self._entries[connector_id]
        entry.retired = True
        if entry.leases == 0:
            await self._close(entry)

    async def _evict(self) -> None:
        idle = sorted(
            ((key, e) for key, e in self._entries.items() if e.leases == 0),
            key=lambda item: item[1].last_used,
        )
        for key, entry in idle[: max(0, len(self._entries) - self._max_size)]:
            await self._retire(key, entry)

    @staticmethod
    async def _close(entry: _Pooled) -> None:
        # An instance from a loop that has gone cannot be closed from this one;
        # its sockets went with that loop.
        if entry.loop is not asyncio.get_running_loop():
            return
        with suppress(Exception):
            await entry.storage.aclose()

    async def aclose(self) -> None:
        """Close every idle instance; leased ones close when released."""
        for key, entry in list(self._entries.items()):
            await self._retire(key, entry)


_pool = ConnectorPool()


def connector_pool() -> ConnectorPool:
    return _pool


@asynccontextmanager
async def storage_for(connector: Connector) -> AsyncIterator[StorageConnector]:
    """`async with storage_for(row) as storage:` — a pooled instance, leased.

    Do not close it: the pool owns it (see the module docstring).
    """
    async with _pool.lease(connector) as storage:
        yield storage


async def aclose_storage() -> None:
    """Close the pooled connectors at shutdown (API lifespan, worker shutdown)."""
    await _pool.aclose()


def check_pdf_mode(settings: Mapping[str, Any] | None, result_connector_id: UUID | None) -> None:
    """`settings.pdf_mode: text` needs a result connector for the extracted text (422)."""
    if (settings or {}).get("pdf_mode") == "text" and result_connector_id is None:
        raise ValidationFailedError(
            "settings.pdf_mode 'text' needs a result connector: the extracted text is stored there."
        )


async def check_project_connectors(
    session: AsyncSession,
    organization_id: UUID,
    *,
    source_connector_id: UUID | None = None,
    result_connector_id: UUID | None = None,
    cache_connector_id: UUID | None = None,
) -> None:
    """Refuse a connector of another organisation (404, never 403) or a read-only result/cache."""
    for connector_id, role in (
        (source_connector_id, "source"),
        (result_connector_id, "result"),
        (cache_connector_id, "cache"),
    ):
        if connector_id is None:
            continue
        connector = await session.get(Connector, connector_id)
        if connector is None or connector.organization_id != organization_id:
            raise NotFoundError(f"Connector {connector_id} does not exist.")
        if role != "source" and connector.type in READ_ONLY_CONNECTOR_TYPES:
            raise ValidationFailedError(
                f"A {connector.type.value} connector is read-only and cannot be the {role} "
                "connector; use it as the source."
            )
