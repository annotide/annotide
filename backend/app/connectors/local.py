"""Local disk / mounted volume storage connector.

Used for development and on-prem deployments (`connector_type = "local"`).
Local disk has no native signed-URL mechanism, so ``signed_url`` returns an
API-proxy URL that the backend itself serves and authorises.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import mimetypes
import os
import tempfile
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import ClassVar
from urllib.parse import quote

from app.connectors.base import BaseStorageConnector, ConnectorCheck, ObjectInfo
from app.connectors.errors import ConnectorError, ConnectorNotFound
from app.core.security import sign_storage_path

_HASH_CHUNK_SIZE = 1024 * 1024


class LocalConnector(BaseStorageConnector):
    """Connector backed by a directory on local disk or a mounted volume."""

    type: ClassVar[str] = "local"

    def __init__(
        self,
        root: Path,
        connector_id: str | None = None,
        public_base_url: str | None = None,
        internal_base_url: str | None = None,
    ) -> None:
        self._root = Path(root)
        self._root_resolved = self._root.resolve()
        # The row id this connector was built from. `signed_url` needs it:
        # the proxy route has to find the same root again, and the signature
        # is bound to it so a URL cannot be replayed against another
        # connector that happens to share a path. `services.storage` injects
        # it; a hand-built connector (tests, scripts) may leave it unset and
        # simply cannot sign.
        self._connector_id = connector_id
        # Absolute origin for consumers that are not the browser — the model
        # service fetching media from inside the compose network. Unset means
        # a root-relative URL, which is what the SPA wants.
        self._public_base_url = (public_base_url or "").rstrip("/")
        # How a model service reaches the API (`APP_INTERNAL_API_URL`), for
        # `internal` URLs. Unset falls back to the public base.
        self._internal_base_url = (internal_base_url or "").rstrip("/")

    def _resolve(self, path: str) -> Path:
        """Resolve a relative object path to an absolute filesystem path.

        Rejects absolute paths, ``..`` segments, and — after following
        symlinks — any path that resolves outside the connector root
        (SEC-4: path traversal / SSRF surface).
        """
        if not path:
            raise ConnectorError("path must not be empty")
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts:
            raise ConnectorError(f"path escapes connector root: {path!r}")
        candidate = (self._root / Path(*pure.parts)).resolve()
        if candidate != self._root_resolved and self._root_resolved not in candidate.parents:
            raise ConnectorError(f"path escapes connector root: {path!r}")
        return candidate

    def _to_relative_posix(self, absolute: Path) -> str:
        return absolute.relative_to(self._root_resolved).as_posix()

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(_HASH_CHUNK_SIZE):
                digest.update(chunk)
        return digest.hexdigest()

    def _list_sync(self, prefix: str, glob: str | None) -> list[ObjectInfo]:
        if not self._root_resolved.is_dir():
            return []
        results: list[ObjectInfo] = []
        for dirpath, _dirnames, filenames in os.walk(self._root_resolved):
            for filename in filenames:
                absolute = Path(dirpath) / filename
                relative = self._to_relative_posix(absolute)
                if not self._matches_prefix_and_glob(relative, prefix, glob):
                    continue
                stat = absolute.stat()
                content_type, _ = mimetypes.guess_type(relative)
                results.append(
                    ObjectInfo(
                        path=relative,
                        size_bytes=stat.st_size,
                        etag=self._hash_file(absolute),
                        last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                        content_type=content_type,
                    )
                )
        results.sort(key=lambda info: info.path)
        return results

    async def list(self, prefix: str, glob: str | None = None) -> AsyncIterator[ObjectInfo]:
        entries = await asyncio.to_thread(self._list_sync, prefix, glob)
        for entry in entries:
            yield entry

    def _read_sync(self, resolved: Path, start: int | None, end: int | None) -> bytes:
        if not resolved.is_file():
            raise ConnectorNotFound(f"object not found: {self._to_relative_posix(resolved)!r}")
        begin = start or 0
        with resolved.open("rb") as handle:
            handle.seek(begin)
            if end is None:
                return handle.read()
            return handle.read(max(end - begin, 0))

    async def read(self, path: str, start: int | None = None, end: int | None = None) -> bytes:
        resolved = self._resolve(path)
        return await asyncio.to_thread(self._read_sync, resolved, start, end)

    def _size_sync(self, resolved: Path) -> int:
        if not resolved.is_file():
            raise ConnectorNotFound(f"object not found: {self._to_relative_posix(resolved)!r}")
        return resolved.stat().st_size

    async def size(self, path: str) -> int:
        return await asyncio.to_thread(self._size_sync, self._resolve(path))

    def _write_sync(self, resolved: Path, data: bytes) -> None:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=resolved.parent, prefix=f".{resolved.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, resolved)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    async def write(self, path: str, data: bytes, content_type: str) -> None:
        # content_type is accepted for interface compatibility; local disk has
        # no metadata store, so it is not persisted (unlike blob backends).
        resolved = self._resolve(path)
        await asyncio.to_thread(self._write_sync, resolved, data)

    def _delete_sync(self, resolved: Path) -> None:
        if not resolved.is_file():
            raise ConnectorNotFound(f"object not found: {self._to_relative_posix(resolved)!r}")
        resolved.unlink()

    async def delete(self, path: str) -> None:
        resolved = self._resolve(path)
        await asyncio.to_thread(self._delete_sync, resolved)

    async def signed_url(
        self, path: str, expires_in: int = 900, write: bool = False, internal: bool = False
    ) -> str:
        """A signed URL for `GET` / `PUT /api/v1/storage/local/{connector_id}/{path}`.

        Local disk has no native signing, so the API proxies it. The query
        string carries an expiry and an HMAC over (connector, path, expiry):
        the proxy is fetched by an `<img>` tag with no credentials, so that
        signature is the only authorisation it gets (AUTH-6, SEC-4). A
        `write` URL carries a write-scoped signature that only the `PUT`
        route accepts. An `internal` URL starts with `APP_INTERNAL_API_URL`
        when that is set, so a model service can fetch it; otherwise both
        kinds are root-relative (or `public_base_url`-based).
        """
        # Validate the path (raises on traversal) before handing back a URL
        # the API layer will later resolve through this same connector.
        self._resolve(path)
        if not self._connector_id:
            raise ConnectorError("local connector needs 'connector_id' to sign URLs")
        expires = int(time.time()) + expires_in
        try:
            signature = sign_storage_path(self._connector_id, path, expires, write=write)
        except RuntimeError as exc:  # no APP_SECRET_KEY: nothing can be signed
            raise ConnectorError("APP_SECRET_KEY must be set to sign local media URLs") from exc
        quoted = quote(path, safe="/")
        base = (internal and self._internal_base_url) or self._public_base_url
        return (
            f"{base}/api/v1/storage/local/{self._connector_id}/{quoted}"
            f"?expires={expires}&sig={signature}"
        )

    def _check_sync(self) -> ConnectorCheck:
        messages: list[str] = []
        if not self._root_resolved.exists():
            message = f"root does not exist: {self._root_resolved}"
            return ConnectorCheck(ok=False, messages=[message])
        if not self._root_resolved.is_dir():
            message = f"root is not a directory: {self._root_resolved}"
            return ConnectorCheck(ok=False, messages=[message])
        if not os.access(self._root_resolved, os.R_OK):
            messages.append(f"root is not readable: {self._root_resolved}")
        if not os.access(self._root_resolved, os.W_OK):
            messages.append(f"root is not writable: {self._root_resolved}")
        return ConnectorCheck(ok=not messages, messages=messages or ["local root is reachable"])

    async def check(self) -> ConnectorCheck:
        return await asyncio.to_thread(self._check_sync)
