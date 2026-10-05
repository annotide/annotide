"""Azure Blob Storage connector (`connector_type = "azure_blob"`).

Supports the Azure-relevant members of the contract's `connector_identity`
enum: ``managed_identity``, ``service_principal``, ``account_key`` and
``sas_token``. Secrets (a service principal's client secret, an account key,
or a SAS token) arrive as already-resolved plain strings — the caller is
responsible for resolving a connector's ``secret_ref`` from Key Vault first.
This module never logs a secret, a generated SAS token, or a URL query
string.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import quote, urlparse

from azure.core.credentials import AzureSasCredential
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import ClientAuthenticationError, ResourceNotFoundError
from azure.identity.aio import (
    ClientSecretCredential,
    DefaultAzureCredential,
    ManagedIdentityCredential,
)
from azure.storage.blob import BlobSasPermissions, ContentSettings, generate_blob_sas
from azure.storage.blob.aio import BlobServiceClient

from app.connectors.base import BaseStorageConnector, ConnectorCheck, ObjectInfo
from app.connectors.errors import ConnectorAuthError, ConnectorConfigError, ConnectorNotFound

#: Identity types this connector accepts (the Azure-relevant subset of the
#: contract's `connector_identity` enum).
SUPPORTED_IDENTITY_TYPES = frozenset(
    {"managed_identity", "service_principal", "account_key", "sas_token"}
)

#: Refresh the cached user delegation key this many seconds before it
#: actually expires, so a signed URL is never generated against a key that
#: expires mid-request.
_DELEGATION_KEY_SAFETY_MARGIN_SECONDS = 60

#: How long a fetched user delegation key stays valid for reuse.
_DELEGATION_KEY_LIFETIME = timedelta(hours=1)


class AzureBlobConnector(BaseStorageConnector):
    """Connector backed by an Azure Blob Storage container."""

    type: ClassVar[str] = "azure_blob"

    def __init__(
        self,
        account_url: str,
        container: str,
        identity_type: str,
        *,
        secret: str | None = None,
        tenant_id: str | None = None,
        client_id: str | None = None,
        frontend_origin: str | None = None,
        public_account_url: str | None = None,
        account_name: str | None = None,
    ) -> None:
        self._account_url = account_url.rstrip("/")
        # The URL a browser must use. It differs from `account_url` whenever the
        # API reaches storage over a private name the browser cannot resolve —
        # a compose service name, a Private Endpoint, an emulator. A signed URL
        # built from the internal name is fetched by the browser, not the API,
        # so it simply fails to load with no server-side error to notice.
        self._public_account_url = (public_account_url or account_url).rstrip("/")
        self._account_name = account_name or self._parse_account_name(self._account_url)
        self._container = container
        self._identity_type = identity_type
        self._secret = secret
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._frontend_origin = frontend_origin

        self._client: BlobServiceClient | None = None
        self._delegation_key: Any | None = None
        self._delegation_key_expiry: datetime | None = None

    @staticmethod
    def _parse_account_name(account_url: str) -> str:
        """Extract the storage account name from a service URL.

        Azure uses host-style URLs (``https://acme.blob.core.windows.net``),
        where the account is the first label of the hostname. Emulators such as
        Azurite use path-style (``http://127.0.0.1:10000/devstoreaccount1``),
        where it is the first path segment instead. Reading the hostname there
        yields ``127.0.0.1`` and every generated SAS is signed for the wrong
        account — which fails as an opaque 403, so it is worth getting right.
        """
        parsed = urlparse(account_url)
        host = parsed.hostname or ""
        path_segments = [segment for segment in parsed.path.split("/") if segment]

        if host.endswith(".core.windows.net") or host.endswith(".blob.core.windows.net"):
            return host.split(".")[0]
        if path_segments:
            return path_segments[0]
        return host.split(".")[0]

    def _build_credential(
        self,
    ) -> dict[str, str] | AsyncTokenCredential | AzureSasCredential:
        """Build the credential to hand to `BlobServiceClient`.

        Raises :class:`ConnectorConfigError` for any identity type / field
        combination that cannot be used to authenticate.
        """
        if self._identity_type == "managed_identity":
            if self._client_id:
                return ManagedIdentityCredential(client_id=self._client_id)
            return DefaultAzureCredential()

        if self._identity_type == "service_principal":
            if not (self._tenant_id and self._client_id and self._secret):
                raise ConnectorConfigError(
                    "service_principal identity requires tenant_id, client_id and secret"
                )
            return ClientSecretCredential(
                tenant_id=self._tenant_id,
                client_id=self._client_id,
                client_secret=self._secret,
            )

        if self._identity_type == "account_key":
            if not self._secret:
                raise ConnectorConfigError("account_key identity requires a secret")
            # The dict form states the account name explicitly. Handing the SDK
            # a bare key makes it infer the account from the URL hostname, which
            # only works for host-style Azure URLs — against a path-style
            # emulator endpoint it raises "Unable to determine account name".
            return {"account_name": self._account_name, "account_key": self._secret}

        if self._identity_type == "sas_token":
            if not self._secret:
                raise ConnectorConfigError("sas_token identity requires a secret")
            return AzureSasCredential(self._secret)

        raise ConnectorConfigError(
            f"unsupported identity_type for azure_blob: {self._identity_type!r}"
        )

    async def _get_client(self) -> BlobServiceClient:
        if self._client is None:
            credential = self._build_credential()
            self._client = BlobServiceClient(account_url=self._account_url, credential=credential)
        return self._client

    def _blob_url(self, path: str, *, public: bool = False) -> str:
        """The blob's URL. `public=True` for anything handed to a browser."""
        base = self._public_account_url if public else self._account_url
        return f"{base}/{self._container}/{quote(path, safe='/')}"

    async def aclose(self) -> None:
        """Release the SDK client and credential.

        `BlobServiceClient` owns an aiohttp session. A connector is built per
        request, so without this every item view leaks a session and its
        connection pool until the process is restarted.
        """
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

        credential = getattr(self, "_owned_credential", None)
        if credential is not None and hasattr(credential, "close"):
            with contextlib.suppress(Exception):
                await credential.close()
            self._owned_credential = None

    async def list(self, prefix: str, glob: str | None = None) -> AsyncIterator[ObjectInfo]:
        client = await self._get_client()
        container_client = client.get_container_client(self._container)
        normalised = self._normalise_prefix(prefix)
        async for blob in container_client.list_blobs(name_starts_with=normalised or None):
            if not self._matches_prefix_and_glob(blob.name, prefix, glob):
                continue
            content_type = blob.content_settings.content_type if blob.content_settings else None
            yield ObjectInfo(
                path=blob.name,
                size_bytes=blob.size or 0,
                etag=blob.etag.strip('"') if blob.etag else None,
                last_modified=blob.last_modified,
                content_type=content_type,
            )

    async def read(self, path: str, start: int | None = None, end: int | None = None) -> bytes:
        client = await self._get_client()
        blob_client = client.get_blob_client(container=self._container, blob=path)
        try:
            if start is None and end is None:
                downloader = await blob_client.download_blob()
            else:
                offset = start or 0
                length = None if end is None else max(end - offset, 0)
                downloader = await blob_client.download_blob(offset=offset, length=length)
            return await downloader.readall()
        except ResourceNotFoundError as exc:
            raise ConnectorNotFound(f"blob not found: {path!r}") from exc
        except ClientAuthenticationError as exc:
            raise ConnectorAuthError(f"authentication failed for blob {path!r}") from exc

    async def write(self, path: str, data: bytes, content_type: str) -> None:
        client = await self._get_client()
        blob_client = client.get_blob_client(container=self._container, blob=path)
        try:
            await blob_client.upload_blob(
                data,
                overwrite=True,
                content_settings=ContentSettings(content_type=content_type),
            )
        except ClientAuthenticationError as exc:
            raise ConnectorAuthError(f"authentication failed writing blob {path!r}") from exc

    async def delete(self, path: str) -> None:
        client = await self._get_client()
        blob_client = client.get_blob_client(container=self._container, blob=path)
        try:
            await blob_client.delete_blob()
        except ResourceNotFoundError as exc:
            raise ConnectorNotFound(f"blob not found: {path!r}") from exc
        except ClientAuthenticationError as exc:
            raise ConnectorAuthError(f"authentication failed deleting blob {path!r}") from exc

    async def _get_user_delegation_key(
        self, client: BlobServiceClient, now: datetime, key_expiry: datetime
    ) -> Any:
        margin = timedelta(seconds=_DELEGATION_KEY_SAFETY_MARGIN_SECONDS)
        if (
            self._delegation_key is not None
            and self._delegation_key_expiry is not None
            and self._delegation_key_expiry - margin > now
        ):
            return self._delegation_key

        key = await client.get_user_delegation_key(key_start_time=now, key_expiry_time=key_expiry)
        self._delegation_key = key
        self._delegation_key_expiry = key_expiry
        return key

    async def signed_url(
        self, path: str, expires_in: int = 900, write: bool = False, internal: bool = False
    ) -> str:
        now = datetime.now(UTC)
        expiry = now + timedelta(seconds=expires_in)
        permission = BlobSasPermissions(read=True, write=write)

        if self._identity_type == "sas_token":
            if not self._secret:
                raise ConnectorConfigError("sas_token identity requires a secret")
            # Already a scoped SAS issued by an administrator; reuse as-is.
            return f"{self._blob_url(path, public=not internal)}?{self._secret}"

        if self._identity_type == "account_key":
            if not self._secret:
                raise ConnectorConfigError("account_key identity requires a secret")
            sas = generate_blob_sas(
                account_name=self._account_name,
                container_name=self._container,
                blob_name=path,
                account_key=self._secret,
                permission=permission,
                expiry=expiry,
            )
            return f"{self._blob_url(path, public=not internal)}?{sas}"

        # managed_identity / service_principal: user delegation SAS.
        client = await self._get_client()
        key_expiry = now + _DELEGATION_KEY_LIFETIME
        key = await self._get_user_delegation_key(client, now, key_expiry)
        sas = generate_blob_sas(
            account_name=self._account_name,
            container_name=self._container,
            blob_name=path,
            user_delegation_key=key,
            permission=permission,
            expiry=expiry,
        )
        return f"{self._blob_url(path, public=not internal)}?{sas}"

    async def check(self) -> ConnectorCheck:
        try:
            client = await self._get_client()
            container_client = client.get_container_client(self._container)
            await container_client.get_container_properties()
        except ResourceNotFoundError:
            return ConnectorCheck(ok=False, messages=[f"container not found: {self._container!r}"])
        except ClientAuthenticationError as exc:
            return ConnectorCheck(
                ok=False, messages=[f"authentication failed: {exc.__class__.__name__}"]
            )
        except ConnectorConfigError as exc:
            return ConnectorCheck(ok=False, messages=[str(exc)])

        messages = [f"container {self._container!r} is reachable"]

        if self._frontend_origin:
            messages.append(await self._check_cors(client))

        return ConnectorCheck(ok=True, messages=messages)

    async def _check_cors(self, client: BlobServiceClient) -> str:
        try:
            properties = await client.get_service_properties()
        except Exception:
            return (
                "could not read CORS rules with the current identity; "
                "verify the storage account's CORS configuration manually (SRC-7)"
            )

        cors_rules = properties.get("cors", []) if isinstance(properties, dict) else []
        matching = [
            rule
            for rule in cors_rules
            if self._frontend_origin in rule.allowed_origins or "*" in rule.allowed_origins
        ]
        if not matching:
            return (
                f"CORS warning: frontend origin {self._frontend_origin!r} is not in the "
                "storage account's allowed origins (SRC-7)"
            )
        # Browser uploads PUT straight to the container with `x-ms-blob-type`
        # (§12 upload path); a read-only CORS rule makes every upload fail
        # with an opaque network error, so say so here rather than there.
        can_put = any(
            "PUT" in {method.upper() for method in getattr(rule, "allowed_methods", [])}
            for rule in matching
        )
        if can_put:
            return f"CORS allows frontend origin {self._frontend_origin!r} (GET and PUT)"
        return (
            f"CORS allows frontend origin {self._frontend_origin!r} for reads only; "
            "add PUT (and the x-ms-blob-type header) to allow browser uploads (SRC-7)"
        )
