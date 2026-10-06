"""Storage connector interface (SRC-1, ARC-5).

Path conventions
-----------------
Every path a connector accepts or returns is a **POSIX-style, relative**
path: forward slashes only, no leading ``/``, no ``.`` or ``..`` segments,
UTF-8, case-sensitive. It is relative to the connector's own root — a
filesystem directory for :class:`~app.connectors.local.LocalConnector`, a
container for :class:`~app.connectors.azure_blob.AzureBlobConnector`, and so
on. Connectors never see or store an absolute filesystem path or a full
``https://`` URL as an item's ``path``.

A ``prefix`` passed to :meth:`StorageConnector.list` is a directory-style
filter: ``"images/"`` and ``"images"`` are equivalent, and only paths that
start with that prefix (after normalisation) are considered. An empty
prefix (``""``) matches everything under the connector root.

A ``glob`` passed to :meth:`StorageConnector.list` is a shell-style pattern
(:mod:`fnmatch` semantics, e.g. ``*.jpg`` or ``**/*.png``) matched against
the *full* relative path of each candidate object, not just its final
segment. It is applied client-side, after the prefix filter narrows the
listing.

Byte ranges passed to :meth:`StorageConnector.read` follow Python slice
semantics: ``start`` is inclusive and defaults to the beginning of the
object, ``end`` is exclusive and defaults to the end of the object.

This module deliberately has no dependency on ``app.core`` or
``app.models`` — see ``docs/CONTRACTS.md`` for the layering rule
(``connectors/`` imports ``core/`` + ``schemas/`` only, and even that is not
needed here). Connectors take their configuration as explicit constructor
arguments so they can be constructed and tested in isolation.
"""

from __future__ import annotations

import fnmatch
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """Metadata for a single object returned by :meth:`StorageConnector.list`."""

    path: str
    size_bytes: int
    etag: str | None
    last_modified: datetime | None
    content_type: str | None


@runtime_checkable
class SizedConnector(Protocol):
    """A connector that can report an object's size without reading it.

    The signed media proxy (`local`, `databricks_volume`) needs the size to
    answer `Range` requests, so a browser can seek in audio and video.
    """

    async def size(self, path: str) -> int: ...


@dataclass(frozen=True, slots=True)
class ConnectorCheck:
    """Result of :meth:`StorageConnector.check`, used by the UI's "test connection"."""

    ok: bool
    messages: list[str]


@runtime_checkable
class StorageConnector(Protocol):
    """The storage plugin interface every connector implements (SRC-1)."""

    type: ClassVar[str]

    # Not `async def`: implementations are async generators, which are
    # iterated directly rather than awaited. Declaring this `async` types it
    # as a coroutine that returns an iterator, and every caller then fails
    # type-checking. The abstract base class below has always had it right.
    def list(self, prefix: str, glob: str | None = None) -> AsyncIterator[ObjectInfo]: ...

    async def read(self, path: str, start: int | None = None, end: int | None = None) -> bytes: ...

    async def write(self, path: str, data: bytes, content_type: str) -> None: ...

    async def delete(self, path: str) -> None: ...

    async def signed_url(
        self, path: str, expires_in: int = 900, write: bool = False, internal: bool = False
    ) -> str: ...

    async def check(self) -> ConnectorCheck: ...

    async def aclose(self) -> None: ...


class BaseStorageConnector(ABC):
    """Shared behaviour for concrete connectors.

    Concrete connectors implement the six :class:`StorageConnector` protocol
    methods (declared here as abstract methods, so a connector cannot be
    instantiated with any of them missing), and reuse the
    prefix-normalisation and glob-matching helpers below so every connector
    applies the same path conventions.
    """

    type: ClassVar[str]

    async def aclose(self) -> None:
        """Release any resources this connector holds.

        A no-op by default: most connectors hold nothing. Ones that own an SDK
        client with a connection pool (Azure Blob) override it. Callers always
        call it, so it must be safe on every connector and safe to call twice.
        """
        return None

    @abstractmethod
    def list(self, prefix: str, glob: str | None = None) -> AsyncIterator[ObjectInfo]:
        """List objects under `prefix`, optionally filtered by a glob pattern.

        Implemented as an ``async def ... yield`` async generator in
        concrete connectors, so it is called directly (``async for obj in
        connector.list(prefix)``) without an extra ``await`` on the call
        itself — hence this abstract declaration is a plain (non-``async``)
        method returning ``AsyncIterator[ObjectInfo]``.
        """

    @abstractmethod
    async def read(self, path: str, start: int | None = None, end: int | None = None) -> bytes:
        """Read an object, or a byte range of it (`start` inclusive, `end` exclusive)."""

    @abstractmethod
    async def write(self, path: str, data: bytes, content_type: str) -> None:
        """Write (creating or overwriting) an object."""

    @abstractmethod
    async def delete(self, path: str) -> None:
        """Delete an object."""

    @abstractmethod
    async def signed_url(
        self, path: str, expires_in: int = 900, write: bool = False, internal: bool = False
    ) -> str:
        """Return a time-limited URL for direct (read, or read/write) access.

        Addressed for a browser unless `internal`, which addresses it for a
        service inside the deployment (a model endpoint, §8).
        """

    @abstractmethod
    async def check(self) -> ConnectorCheck:
        """Verify the connector can reach its backing store; used by "test connection"."""

    @staticmethod
    def _normalise_prefix(prefix: str) -> str:
        """Normalise a listing prefix: strip leading/trailing slashes.

        ``"images/"``, ``"/images/"`` and ``"images"`` all normalise to
        ``"images"``; ``""`` and ``"/"`` both normalise to ``""`` (list
        everything).
        """
        return prefix.strip("/")

    @classmethod
    def _matches_prefix_and_glob(cls, path: str, prefix: str, glob: str | None) -> bool:
        """Return ``True`` if `path` is under `prefix` and matches `glob` (if given)."""
        normalised_prefix = cls._normalise_prefix(prefix)
        if normalised_prefix and not (
            path == normalised_prefix or path.startswith(normalised_prefix + "/")
        ):
            return False
        return glob is None or fnmatch.fnmatch(path, glob)
