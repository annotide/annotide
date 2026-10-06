"""SharePoint document libraries and OneDrive (`connector_type = "sharepoint"`, §3).

Talks to Microsoft Graph v1.0 with `httpx`; a drive is a SharePoint document
library or a OneDrive, addressed by `drive_id`, and every path is relative
to the drive root. Sign-in is the client-credentials flow of an app
registration (`service_principal`, the resolved secret is its client
secret) or the Azure managed identity (`managed_identity`), both through
`azure-identity`, for the scope `https://graph.microsoft.com/.default`.

The browser reads media from Graph's own `@microsoft.graph.downloadUrl`, a
pre-authenticated URL that lives about an hour (ARC-3: the platform never
proxies it). Graph has no single-request upload URL the browser upload can
use, so `signed_url(write=True)` is unsupported; the platform's own writes
(results, exports) go through `write`. See CONTRACTS.md.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Any, ClassVar, NoReturn
from urllib.parse import quote

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

GRAPH_URL = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
SUPPORTED_IDENTITY_TYPES = frozenset({"service_principal", "managed_identity"})
#: Graph's limit for a single-request upload; above it, an upload session.
SIMPLE_UPLOAD_MAX = 250 * 1024 * 1024
#: Upload-session chunks must be a multiple of 320 KiB.
UPLOAD_CHUNK = 32 * 320 * 1024
_RETRY_STATUSES = frozenset({429, 503})
_MAX_RETRIES = 3
_PAGE = 200

TokenProvider = Callable[[], Awaitable[str]]


def _graph_path(path: str) -> str:
    return quote(path.strip("/"), safe="/")


class SharePointConnector(BaseStorageConnector):
    """Connector backed by one SharePoint / OneDrive drive."""

    type: ClassVar[str] = "sharepoint"

    def __init__(
        self,
        *,
        drive_id: str,
        identity_type: str,
        secret: str | None = None,
        tenant_id: str | None = None,
        client_id: str | None = None,
        graph_url: str = GRAPH_URL,
        token_provider: TokenProvider | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if identity_type not in SUPPORTED_IDENTITY_TYPES and token_provider is None:
            raise ConnectorConfigError(
                f"sharepoint identity_type must be one of {sorted(SUPPORTED_IDENTITY_TYPES)}"
            )
        if (
            identity_type == "service_principal"
            and token_provider is None
            and not (tenant_id and client_id and secret)
        ):
            raise ConnectorConfigError(
                "service_principal needs tenant_id, client_id and a client secret"
            )
        self._drive = quote(drive_id, safe="")
        self._identity_type = identity_type
        self._secret = secret
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._token_provider = token_provider
        self._credential: Any = None
        self._sleep = sleep
        self._client = httpx.AsyncClient(
            base_url=graph_url.rstrip("/"),
            transport=transport,
            timeout=60.0,
            follow_redirects=True,
        )
        # Pre-authenticated upload / download URLs must not get our token.
        # ... and may only be redirected to public https hosts (SEC-4).
        self._bare = httpx.AsyncClient(
            transport=transport,
            timeout=300.0,
            follow_redirects=True,
            event_hooks={"response": [redirect_guard(https_only=True)]},
        )

    # -- auth ---------------------------------------------------------------

    async def _token(self) -> str:
        if self._token_provider is not None:
            return await self._token_provider()
        if self._credential is None:
            from azure.identity.aio import ClientSecretCredential, DefaultAzureCredential

            if self._identity_type == "service_principal":
                self._credential = ClientSecretCredential(
                    str(self._tenant_id), str(self._client_id), str(self._secret)
                )
            else:
                self._credential = DefaultAzureCredential(
                    managed_identity_client_id=self._client_id
                )
        try:
            token = await self._credential.get_token(GRAPH_SCOPE)
        except Exception as exc:  # azure-identity raises its own hierarchy
            raise ConnectorAuthError("could not get a Microsoft Graph token") from exc
        return str(token.token)

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._bare.aclose()
        if self._credential is not None:
            await self._credential.close()
            self._credential = None

    # -- HTTP ---------------------------------------------------------------

    async def _request(self, method: str, url: str, *, what: str, **kwargs: Any) -> httpx.Response:
        headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {await self._token()}"}
        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await self._client.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise ConnectorError(f"could not reach Microsoft Graph for {what}") from exc
            if response.status_code in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                await self._sleep(float(response.headers.get("Retry-After", 2**attempt)))
                continue
            if response.is_success:
                return response
            self._raise(response, what)
        raise ConnectorError(f"Microsoft Graph kept throttling {what}")  # pragma: no cover

    @staticmethod
    def _raise(response: httpx.Response, what: str) -> NoReturn:
        if response.status_code == 404:
            raise ConnectorNotFound(f"not found: {what}")
        if response.status_code in (401, 403):
            raise ConnectorAuthError(f"not allowed: {what} ({response.status_code})")
        raise ConnectorError(f"Microsoft Graph answered {response.status_code} for {what}")

    def _item_url(self, path: str) -> str:
        clean = path.strip("/")
        if not clean:
            return f"/drives/{self._drive}/root"
        return f"/drives/{self._drive}/root:/{_graph_path(clean)}:"

    # -- the interface ------------------------------------------------------

    async def list(self, prefix: str, glob: str | None = None) -> AsyncIterator[ObjectInfo]:
        start = self._normalise_prefix(prefix)
        folders = [start]
        while folders:
            folder = folders.pop(0)
            url: str | None = (
                f"{self._item_url(folder)}/children?$top={_PAGE}"
                "&$select=name,size,eTag,lastModifiedDateTime,file,folder"
            )
            while url:
                try:
                    response = await self._request("GET", url, what=f"folder {folder or '/'}")
                except ConnectorNotFound:
                    break  # a prefix that names no folder lists nothing
                page = response.json()
                for entry in page.get("value", []):
                    path = f"{folder}/{entry['name']}" if folder else str(entry["name"])
                    if "folder" in entry:
                        folders.append(path)
                    elif "file" in entry and self._matches_prefix_and_glob(path, prefix, glob):
                        yield ObjectInfo(
                            path=path,
                            size_bytes=int(entry.get("size") or 0),
                            etag=entry.get("eTag"),
                            last_modified=_parse_time(entry.get("lastModifiedDateTime")),
                            content_type=(entry.get("file") or {}).get("mimeType"),
                        )
                url = page.get("@odata.nextLink")

    async def read(self, path: str, start: int | None = None, end: int | None = None) -> bytes:
        headers: dict[str, str] = {}
        if start is not None or end is not None:
            first = start or 0
            headers["Range"] = f"bytes={first}-{'' if end is None else end - 1}"
        response = await self._request(
            "GET", f"{self._item_url(path)}/content", what=f"file {path!r}", headers=headers
        )
        return response.content

    async def write(self, path: str, data: bytes, content_type: str) -> None:
        if len(data) <= SIMPLE_UPLOAD_MAX:
            await self._request(
                "PUT",
                f"{self._item_url(path)}/content",
                what=f"upload of {path!r}",
                content=data,
                headers={"Content-Type": content_type},
            )
            return
        session = await self._request(
            "POST",
            f"{self._item_url(path)}/createUploadSession",
            what=f"upload session for {path!r}",
            json={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
        )
        upload_url = str(session.json()["uploadUrl"])
        total = len(data)
        for offset in range(0, total, UPLOAD_CHUNK):
            chunk = data[offset : offset + UPLOAD_CHUNK]
            try:
                response = await self._bare.put(
                    upload_url,
                    content=chunk,
                    headers={"Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{total}"},
                )
            except httpx.HTTPError as exc:
                raise ConnectorError(f"upload of {path!r} failed: {exc}") from exc
            if not response.is_success:
                self._raise(response, f"upload of {path!r}")

    async def delete(self, path: str) -> None:
        await self._request("DELETE", self._item_url(path), what=f"file {path!r}")

    async def signed_url(
        self, path: str, expires_in: int = 900, write: bool = False, internal: bool = False
    ) -> str:
        if write:
            raise UnsupportedOperation(
                "SharePoint has no single-request upload URL; upload through the platform"
            )
        response = await self._request(
            "GET",
            f"{self._item_url(path)}?$select=id,@microsoft.graph.downloadUrl",
            what=f"file {path!r}",
        )
        url = response.json().get("@microsoft.graph.downloadUrl")
        if not url:
            raise ConnectorError(f"Graph gave no download URL for {path!r}")
        return str(url)

    async def check(self) -> ConnectorCheck:
        try:
            response = await self._request("GET", f"/drives/{self._drive}", what="the drive")
        except ConnectorError as exc:
            return ConnectorCheck(ok=False, messages=[str(exc)])
        drive = response.json()
        return ConnectorCheck(
            ok=True,
            messages=[f"drive {drive.get('name')!r} ({drive.get('driveType', '?')}) is reachable"],
        )


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_sharepoint(config: dict[str, Any], secret: str | None) -> SharePointConnector:
    if not config.get("drive_id"):
        raise ConnectorConfigError("sharepoint connector config requires 'drive_id'")
    return SharePointConnector(
        drive_id=str(config["drive_id"]),
        identity_type=str(config.get("identity_type", "service_principal")),
        secret=secret,
        tenant_id=config.get("tenant_id"),
        client_id=config.get("client_id"),
        graph_url=str(config.get("graph_url") or GRAPH_URL),
    )
