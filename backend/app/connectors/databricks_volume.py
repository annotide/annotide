"""Databricks Unity Catalog volumes (`connector_type = "databricks_volume"`, §3).

Talks to the workspace's Files API 2.0 with `httpx`. Paths are relative to
the volume (`/Volumes/<catalog>/<schema>/<volume>`). Sign-in is a personal
access token (`access_key`) or a service principal's OAuth secret
(`service_principal`, machine-to-machine, token from `{host}/oidc/v1/token`).

Volumes have no presigned URLs, so `signed_url` points at the platform's own
signed proxy (`/api/v1/storage/proxy/...`), the same HMAC scheme the `local`
connector uses: the one place besides local disk where media passes through
the API (CONTRACTS.md, ARC-3 exception). A volume on an external location in
the customer's own cloud storage is better read through that storage's
connector, which signs natively.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar, NoReturn
from urllib.parse import quote

import httpx

from app.connectors.base import BaseStorageConnector, ConnectorCheck, ObjectInfo
from app.connectors.errors import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorError,
    ConnectorNotFound,
)
from app.core.security import sign_storage_path

SUPPORTED_IDENTITY_TYPES = frozenset({"access_key", "service_principal"})
#: Refresh an OAuth token this long before it expires.
_TOKEN_MARGIN_SECONDS = 60


class DatabricksVolumeConnector(BaseStorageConnector):
    """Connector backed by one Unity Catalog volume."""

    type: ClassVar[str] = "databricks_volume"

    def __init__(
        self,
        *,
        host: str,
        volume_path: str,
        identity_type: str,
        secret: str | None,
        client_id: str | None = None,
        connector_id: str | None = None,
        public_base_url: str | None = None,
        internal_base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not host.startswith("https://") and not host.startswith("http://"):
            raise ConnectorConfigError("databricks_volume 'host' must be an http(s) URL")
        volume = "/" + volume_path.strip("/")
        if not volume.startswith("/Volumes/") or volume.count("/") < 4:
            raise ConnectorConfigError(
                "databricks_volume 'volume_path' must be /Volumes/<catalog>/<schema>/<volume>"
            )
        if identity_type not in SUPPORTED_IDENTITY_TYPES:
            raise ConnectorConfigError(
                f"databricks_volume identity_type must be one of {sorted(SUPPORTED_IDENTITY_TYPES)}"
            )
        if not secret:
            raise ConnectorConfigError("databricks_volume needs a token or OAuth secret")
        if identity_type == "service_principal" and not client_id:
            raise ConnectorConfigError("service_principal needs 'client_id'")
        self._volume = volume.rstrip("/")
        self._identity_type = identity_type
        self._secret = secret
        self._client_id = client_id
        self._connector_id = connector_id
        self._public_base_url = (public_base_url or "").rstrip("/")
        self._internal_base_url = (internal_base_url or "").rstrip("/")
        self._oauth_token: str | None = None
        self._oauth_expires = 0.0
        self._client = httpx.AsyncClient(
            base_url=host.rstrip("/"), transport=transport, timeout=120.0
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- auth ---------------------------------------------------------------

    async def _token(self) -> str:
        if self._identity_type == "access_key":
            return str(self._secret)
        if self._oauth_token and time.time() < self._oauth_expires - _TOKEN_MARGIN_SECONDS:
            return self._oauth_token
        try:
            response = await self._client.post(
                "/oidc/v1/token",
                data={"grant_type": "client_credentials", "scope": "all-apis"},
                auth=(str(self._client_id), str(self._secret)),
            )
        except httpx.HTTPError as exc:
            raise ConnectorError("could not reach the Databricks token endpoint") from exc
        if not response.is_success:
            raise ConnectorAuthError(
                f"Databricks refused the OAuth secret ({response.status_code})"
            )
        body = response.json()
        self._oauth_token = str(body["access_token"])
        self._oauth_expires = time.time() + float(body.get("expires_in", 3600))
        return self._oauth_token

    async def _request(self, method: str, url: str, *, what: str, **kwargs: Any) -> httpx.Response:
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {await self._token()}"}
        try:
            response = await self._client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"could not reach Databricks for {what}") from exc
        if not response.is_success:
            self._raise(response, what)
        return response

    @staticmethod
    def _raise(response: httpx.Response, what: str) -> NoReturn:
        if response.status_code == 404:
            raise ConnectorNotFound(f"not found: {what}")
        if response.status_code in (401, 403):
            raise ConnectorAuthError(f"not allowed: {what} ({response.status_code})")
        raise ConnectorError(f"Databricks answered {response.status_code} for {what}")

    def _file_url(self, path: str) -> str:
        return f"/api/2.0/fs/files{quote(self._volume)}/{quote(path.strip('/'), safe='/')}"

    def _dir_url(self, path: str) -> str:
        suffix = f"/{quote(path.strip('/'), safe='/')}" if path.strip("/") else ""
        return f"/api/2.0/fs/directories{quote(self._volume)}{suffix}/"

    def _relative(self, absolute: str) -> str:
        return absolute[len(self._volume) :].lstrip("/")

    # -- the interface ------------------------------------------------------

    async def list(self, prefix: str, glob: str | None = None) -> AsyncIterator[ObjectInfo]:
        folders = [self._normalise_prefix(prefix)]
        while folders:
            folder = folders.pop(0)
            token: str | None = None
            while True:
                params = {"page_token": token} if token else {}
                try:
                    response = await self._request(
                        "GET",
                        self._dir_url(folder),
                        what=f"directory {folder or '/'}",
                        params=params,
                    )
                except ConnectorNotFound:
                    break
                page = response.json()
                for entry in page.get("contents", []):
                    path = self._relative(str(entry["path"]))
                    if entry.get("is_directory"):
                        folders.append(path)
                    elif self._matches_prefix_and_glob(path, prefix, glob):
                        modified = entry.get("last_modified")
                        yield ObjectInfo(
                            path=path,
                            size_bytes=int(entry.get("file_size") or 0),
                            # Files API gives no ETag; size + mtime detect a change (SRC-4).
                            etag=f"{entry.get('file_size')}-{modified}",
                            last_modified=(
                                datetime.fromtimestamp(int(modified) / 1000, tz=UTC)
                                if modified is not None
                                else None
                            ),
                            content_type=None,
                        )
                token = page.get("next_page_token")
                if not token:
                    break

    async def read(self, path: str, start: int | None = None, end: int | None = None) -> bytes:
        headers: dict[str, str] = {}
        if start is not None or end is not None:
            headers["Range"] = f"bytes={start or 0}-{'' if end is None else end - 1}"
        response = await self._request(
            "GET", self._file_url(path), what=f"file {path!r}", headers=headers
        )
        if headers and response.status_code == 200:
            # The server ignored the Range: slice locally, like the other connectors.
            return response.content[start or 0 : end]
        return response.content

    async def size(self, path: str) -> int:
        response = await self._request("HEAD", self._file_url(path), what=f"file {path!r}")
        try:
            return int(response.headers["Content-Length"])
        except (KeyError, ValueError) as exc:
            raise ConnectorError(f"Databricks sent no size for file {path!r}") from exc

    async def write(self, path: str, data: bytes, content_type: str) -> None:
        await self._request(
            "PUT",
            self._file_url(path),
            what=f"upload of {path!r}",
            params={"overwrite": "true"},
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )

    async def delete(self, path: str) -> None:
        await self._request("DELETE", self._file_url(path), what=f"file {path!r}")

    async def signed_url(
        self, path: str, expires_in: int = 900, write: bool = False, internal: bool = False
    ) -> str:
        if not self._connector_id:
            raise ConnectorError("databricks_volume needs 'connector_id' to sign proxy URLs")
        expires = int(time.time()) + expires_in
        try:
            signature = sign_storage_path(self._connector_id, path, expires, write=write)
        except RuntimeError as exc:
            raise ConnectorError("APP_SECRET_KEY must be set to sign proxy media URLs") from exc
        base = (internal and self._internal_base_url) or self._public_base_url
        return (
            f"{base}/api/v1/storage/proxy/{self._connector_id}/"
            f"{quote(path, safe='/')}?expires={expires}&sig={signature}"
        )

    async def check(self) -> ConnectorCheck:
        try:
            await self._request("GET", self._dir_url(""), what="the volume")
        except ConnectorError as exc:
            return ConnectorCheck(ok=False, messages=[str(exc)])
        return ConnectorCheck(ok=True, messages=[f"volume {self._volume} is reachable"])


def build_databricks_volume(
    config: dict[str, Any], secret: str | None
) -> DatabricksVolumeConnector:
    missing = [key for key in ("host", "volume_path") if not config.get(key)]
    if missing:
        raise ConnectorConfigError(
            f"databricks_volume connector config missing required key(s): {', '.join(missing)}"
        )
    return DatabricksVolumeConnector(
        host=str(config["host"]),
        volume_path=str(config["volume_path"]),
        identity_type=str(config.get("identity_type", "access_key")),
        secret=secret,
        client_id=config.get("client_id"),
        connector_id=config.get("connector_id"),
        public_base_url=config.get("public_base_url"),
        internal_base_url=config.get("internal_base_url"),
    )
