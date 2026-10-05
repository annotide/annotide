"""Read-only HTTP(S) URL-list connector (`http`).

Objects are files under one `base_url` on a plain web server or CDN, named by
a manifest (one path per line) or an inline `paths` list. The connector never
fetches anything outside `base_url`, so a manifest cannot point the worker at
an internal address, and a redirect may not lead from a public host to an
internal one (never to link-local / metadata). There is no signing: a
"signed" URL is the object's own URL, so the files must be readable by the
browser as they are. Writes and deletes are refused, so an `http` connector
can be a source, never a result connector. See docs/CONTRACTS.md → "Storage
connector interface".
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, ClassVar, NoReturn
from urllib.parse import quote, unquote, urlsplit

import httpx

from app.connectors.base import BaseStorageConnector, ConnectorCheck, ObjectInfo
from app.connectors.errors import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorError,
    ConnectorNotFound,
    UnsupportedOperation,
)
from app.core.netguard import redirect_guard

#: Parallel HEAD requests while listing.
_HEAD_CONCURRENCY = 8
_DEFAULT_MANIFEST = "manifest.txt"


def _raise_for(response: httpx.Response, path: str) -> NoReturn:
    if response.status_code == 404:
        raise ConnectorNotFound(f"{path}: not found (HTTP 404)")
    if response.status_code in (401, 403):
        raise ConnectorAuthError(f"{path}: access denied (HTTP {response.status_code})")
    raise ConnectorError(f"{path}: HTTP {response.status_code}")


def _last_modified(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


class HTTPConnector(BaseStorageConnector):
    """Lists and reads files named by a manifest under one HTTP(S) base URL."""

    type: ClassVar[str] = "http"

    def __init__(
        self,
        *,
        base_url: str,
        manifest: str | None = None,
        paths: list[str] | None = None,
        frontend_origin: str | None = None,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ConnectorConfigError(f"http connector base_url must be http(s): {base_url!r}")
        if parts.query or parts.fragment:
            raise ConnectorConfigError("http connector base_url must not carry a query or fragment")
        self._base_url = base_url if base_url.endswith("/") else base_url + "/"
        self._manifest = manifest or _DEFAULT_MANIFEST
        self._paths = paths
        self._frontend_origin = frontend_origin
        # A redirect may not move from a public host to an internal one, and never
        # to link-local / metadata addresses (SEC-4); a base_url that is itself
        # private (a lab, the emulators) may redirect within private space.
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=True,
            transport=transport,
            event_hooks={"response": [redirect_guard(origin_url=self._base_url)]},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- paths ---------------------------------------------------------------

    def _relative(self, entry: str) -> str:
        """A manifest entry as a path under `base_url`; absolute URLs must be under it."""
        entry = entry.strip()
        if "://" in entry:
            if not entry.startswith(self._base_url):
                raise ConnectorConfigError(f"manifest entry is outside base_url: {entry!r}")
            entry = entry[len(self._base_url) :]
        path = unquote(entry.split("?", 1)[0].split("#", 1)[0]).lstrip("/")
        if not path or any(part in ("..", ".") for part in path.split("/")):
            raise ConnectorConfigError(f"invalid manifest entry: {entry!r}")
        return path

    def url_for(self, path: str) -> str:
        """The absolute URL of `path`; refuses anything that would leave `base_url`."""
        return self._base_url + quote(self._relative(path), safe="/~")

    async def _entries(self) -> list[str]:
        if self._paths is not None:
            raw: list[str] = list(self._paths)
        else:
            body = await self._get(self._manifest)
            text = body.decode("utf-8-sig")
            if text.lstrip().startswith(("[", "{")):
                try:
                    data = json.loads(text)
                except ValueError as exc:
                    raise ConnectorConfigError(f"manifest is not valid JSON: {exc}") from exc
                if not isinstance(data, list) or not all(isinstance(e, str) for e in data):
                    raise ConnectorConfigError("a JSON manifest must be an array of strings")
                raw = data
            else:
                raw = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
        seen: dict[str, None] = {}
        for entry in raw:
            if entry.strip():
                seen.setdefault(self._relative(entry), None)
        return list(seen)

    # --- HTTP ----------------------------------------------------------------

    async def _request(
        self, method: str, path: str, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        try:
            return await self._client.request(method, self.url_for(path), headers=headers)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"{path}: {type(exc).__name__}: {exc}") from exc

    async def _get(self, path: str, headers: dict[str, str] | None = None) -> bytes:
        response = await self._request("GET", path, headers)
        if response.status_code >= 400:
            _raise_for(response, path)
        return response.content

    async def _info(self, path: str) -> ObjectInfo | None:
        """Metadata from `HEAD`, or a one-byte ranged `GET` where `HEAD` is refused."""
        response = await self._request("HEAD", path)
        size: int | None = None
        if response.status_code in (405, 501):
            response = await self._request("GET", path, {"Range": "bytes=0-0"})
            total = response.headers.get("content-range", "").rpartition("/")[2]
            size = int(total) if total.isdigit() else None
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            _raise_for(response, path)
        if size is None:
            length = response.headers.get("content-length", "")
            size = int(length) if length.isdigit() else 0
        return ObjectInfo(
            path=path,
            size_bytes=size,
            etag=response.headers.get("etag"),
            last_modified=_last_modified(response.headers.get("last-modified")),
            content_type=response.headers.get("content-type"),
        )

    # --- StorageConnector ----------------------------------------------------

    async def list(self, prefix: str, glob: str | None = None) -> AsyncIterator[ObjectInfo]:
        paths = [p for p in await self._entries() if self._matches_prefix_and_glob(p, prefix, glob)]
        semaphore = asyncio.Semaphore(_HEAD_CONCURRENCY)

        async def info(path: str) -> ObjectInfo | None:
            async with semaphore:
                return await self._info(path)

        for start in range(0, len(paths), 256):
            batch = await asyncio.gather(*(info(p) for p in paths[start : start + 256]))
            for obj in batch:
                if obj is not None:  # listed in the manifest but gone from the server
                    yield obj

    async def read(self, path: str, start: int | None = None, end: int | None = None) -> bytes:
        if start is None and end is None:
            return await self._get(path)
        offset = start or 0
        if end is not None and end <= offset:
            return b""
        header = f"bytes={offset}-{end - 1}" if end is not None else f"bytes={offset}-"
        response = await self._request("GET", path, {"Range": header})
        if response.status_code >= 400:
            _raise_for(response, path)
        if response.status_code == 206:
            return response.content
        return response.content[offset:end]  # the server ignored the range

    async def write(self, path: str, data: bytes, content_type: str) -> None:
        raise UnsupportedOperation("http connector is read-only")

    async def delete(self, path: str) -> None:
        raise UnsupportedOperation("http connector is read-only")

    async def signed_url(
        self, path: str, expires_in: int = 900, write: bool = False, internal: bool = False
    ) -> str:
        if write:
            raise UnsupportedOperation("http connector is read-only")
        return self.url_for(path)

    async def check(self) -> ConnectorCheck:
        messages: list[str] = []
        try:
            entries = await self._entries()
        except ConnectorError as exc:
            return ConnectorCheck(ok=False, messages=[f"could not read the file list: {exc}"])
        if not entries:
            return ConnectorCheck(ok=False, messages=["the file list is empty"])
        messages.append(f"{len(entries)} file(s) listed")
        if self._base_url.startswith("http://"):
            messages.append("warning: base_url is plain http; use https outside a lab")
        try:
            first = await self._info(entries[0])
        except ConnectorError as exc:
            return ConnectorCheck(ok=False, messages=[*messages, f"{entries[0]}: {exc}"])
        if first is None:
            return ConnectorCheck(ok=False, messages=[*messages, f"{entries[0]}: not found"])
        if self._frontend_origin:
            response = await self._request("HEAD", entries[0], {"Origin": self._frontend_origin})
            allowed = response.headers.get("access-control-allow-origin")
            if allowed not in ("*", self._frontend_origin):
                messages.append(
                    f"warning: CORS does not allow {self._frontend_origin}; images show, but "
                    "pixel tools (brush, superpixels) cannot read them"
                )
        return ConnectorCheck(ok=True, messages=messages)


def build_http(config: dict[str, Any]) -> HTTPConnector:
    """Construct an `HTTPConnector` from a `connector` row's config dict."""
    if "base_url" not in config:
        raise ConnectorConfigError("http connector config missing required key(s): base_url")
    paths = config.get("paths")
    if paths is not None and (
        not isinstance(paths, list) or not all(isinstance(p, str) for p in paths)
    ):
        raise ConnectorConfigError("http connector `paths` must be a list of strings")
    return HTTPConnector(
        base_url=str(config["base_url"]),
        manifest=config.get("manifest"),
        paths=paths,
        frontend_origin=config.get("frontend_origin"),
        timeout_seconds=float(config.get("timeout_seconds", 30.0)),
    )
