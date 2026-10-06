"""Google Cloud Storage connector (`connector_type = "gcs"`).

Supports the GCS-relevant members of the contract's `connector_identity`
enum: ``account_key`` (a service-account JSON key resolved as the
connector's secret) and ``managed_identity`` (Application Default
Credentials, signed through the IAM `signBlob` API). ``none`` is also
accepted for anonymous access against an emulator or a public bucket
(tests only, mirroring `s3`'s `none` identity). Secrets arrive as an
already-resolved plain string — the caller is responsible for resolving a
connector's ``secret_ref`` first. This module never logs a secret or a
generated signed URL's query string.

``google-cloud-storage`` is a synchronous SDK; every call that touches the
network runs through `asyncio.to_thread` so the request path stays async.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any, ClassVar
from urllib.parse import quote

from google.api_core.exceptions import Forbidden, NotFound, Unauthorized
from google.auth import default as google_auth_default
from google.auth.credentials import AnonymousCredentials, Credentials
from google.auth.exceptions import DefaultCredentialsError, RefreshError
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.cloud import storage
from google.oauth2 import service_account

from app.connectors.base import BaseStorageConnector, ConnectorCheck, ObjectInfo
from app.connectors.errors import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorNotFound,
    UnsupportedOperation,
)

#: Identity types this connector accepts (the GCS-relevant subset of the
#: contract's `connector_identity` enum, plus `none` for anonymous/emulator
#: access in tests).
SUPPORTED_IDENTITY_TYPES = frozenset({"account_key", "managed_identity", "none"})

#: `google.auth.exceptions.GoogleAuthError` subclasses that mean "the current
#: identity cannot authenticate", mapped to `ConnectorAuthError` everywhere
#: below.
_AUTH_ERRORS = (Forbidden, Unauthorized)

#: Where objects live when no emulator is configured.
_DEFAULT_ACCESS_ENDPOINT = "https://storage.googleapis.com"


class GCSConnector(BaseStorageConnector):
    """Connector backed by a Google Cloud Storage bucket."""

    type: ClassVar[str] = "gcs"

    def __init__(
        self,
        bucket: str,
        identity_type: str,
        *,
        secret: str | None = None,
        project: str | None = None,
        endpoint_url: str | None = None,
        public_endpoint_url: str | None = None,
        frontend_origin: str | None = None,
    ) -> None:
        self._bucket_name = bucket
        self._identity_type = identity_type
        self._secret = secret
        self._project = project
        # An emulator's address, e.g. `http://gcs-emulator:4443`. Optional:
        # against real GCS this stays unset and the SDK's default host applies.
        self._endpoint_url = endpoint_url.rstrip("/") if endpoint_url else None
        # The URL a browser must use. It differs from `endpoint_url` whenever
        # the API reaches storage over a private name the browser cannot
        # resolve — a compose service name or an emulator. A signed URL built
        # from the internal name is fetched by the browser, not the API, so it
        # simply fails to load with no server-side error to notice.
        self._public_endpoint_url = (
            public_endpoint_url.rstrip("/") if public_endpoint_url else self._endpoint_url
        )
        self._frontend_origin = frontend_origin

        self._client: storage.Client | None = None
        self._credentials: Credentials | None = None

    def _build_credentials(self) -> Credentials:
        """Build the credentials to hand to `storage.Client`.

        Raises :class:`ConnectorConfigError` for any identity type / secret
        combination that cannot be used to authenticate.
        """
        if self._identity_type == "account_key":
            if not self._secret:
                raise ConnectorConfigError("account_key identity requires a secret")
            try:
                info = json.loads(self._secret)
            except json.JSONDecodeError as exc:
                raise ConnectorConfigError("account_key secret is not valid JSON") from exc
            # google.oauth2 ships `py.typed` but its own defs are untyped, so
            # mypy strict sees the call below as a call into an untyped function.
            from_service_account_info = service_account.Credentials.from_service_account_info
            try:
                credentials = from_service_account_info(info)  # type: ignore[no-untyped-call]
            except (ValueError, KeyError) as exc:
                raise ConnectorConfigError(
                    f"account_key secret is not a valid service-account key: {exc}"
                ) from exc
            return credentials  # type: ignore[no-any-return]

        if self._identity_type == "managed_identity":
            try:
                credentials, _project = google_auth_default()
            except DefaultCredentialsError as exc:
                raise ConnectorConfigError(
                    "managed_identity identity could not load application default "
                    f"credentials: {exc}"
                ) from exc
            return credentials

        if self._identity_type == "none":
            # google.auth ships `py.typed` but its own defs are untyped, so
            # mypy strict sees this as a call into an untyped function.
            return AnonymousCredentials()  # type: ignore[no-untyped-call]

        raise ConnectorConfigError(f"unsupported identity_type for gcs: {self._identity_type!r}")

    def _build_client(self) -> storage.Client:
        credentials = self._build_credentials()
        self._credentials = credentials
        client_options = {"api_endpoint": self._endpoint_url} if self._endpoint_url else None
        return storage.Client(
            project=self._project,
            credentials=credentials,
            client_options=client_options,
        )

    async def _get_client(self) -> storage.Client:
        if self._client is None:
            self._client = await asyncio.to_thread(self._build_client)
        return self._client

    async def aclose(self) -> None:
        """Release the SDK client's HTTP session.

        A connector is built per request, so without this every item view
        leaks the underlying `requests` session until the process restarts.
        """
        client, self._client = self._client, None
        self._credentials = None
        if client is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(client.close)

    async def list(self, prefix: str, glob: str | None = None) -> AsyncIterator[ObjectInfo]:
        client = await self._get_client()
        normalised = self._normalise_prefix(prefix)
        blobs = await asyncio.to_thread(
            lambda: list(client.list_blobs(self._bucket_name, prefix=normalised or None))
        )
        for blob in blobs:
            if not self._matches_prefix_and_glob(blob.name, prefix, glob):
                continue
            yield ObjectInfo(
                path=blob.name,
                size_bytes=blob.size or 0,
                etag=blob.etag.strip('"') if blob.etag else None,
                last_modified=blob.updated,
                content_type=blob.content_type,
            )

    async def read(self, path: str, start: int | None = None, end: int | None = None) -> bytes:
        client = await self._get_client()
        blob = client.bucket(self._bucket_name).blob(path)
        # The interface's `end` is exclusive (Python slice semantics); GCS's
        # `end` is an inclusive byte offset, matching an HTTP `Range` header.
        gcs_end = None if end is None else end - 1
        try:
            result: bytes = await asyncio.to_thread(
                blob.download_as_bytes, start=start, end=gcs_end
            )
            return result
        except NotFound as exc:
            raise ConnectorNotFound(f"object not found: {path!r}") from exc
        except _AUTH_ERRORS as exc:
            raise ConnectorAuthError(f"authentication failed for object {path!r}") from exc

    async def write(self, path: str, data: bytes, content_type: str) -> None:
        client = await self._get_client()
        blob = client.bucket(self._bucket_name).blob(path)
        try:
            await asyncio.to_thread(blob.upload_from_string, data, content_type=content_type)
        except _AUTH_ERRORS as exc:
            raise ConnectorAuthError(f"authentication failed writing object {path!r}") from exc

    async def delete(self, path: str) -> None:
        client = await self._get_client()
        blob = client.bucket(self._bucket_name).blob(path)
        try:
            await asyncio.to_thread(blob.delete)
        except NotFound as exc:
            raise ConnectorNotFound(f"object not found: {path!r}") from exc
        except _AUTH_ERRORS as exc:
            raise ConnectorAuthError(f"authentication failed deleting object {path!r}") from exc

    def _access_endpoint(self, *, internal: bool) -> str | None:
        """The host a signed URL should be addressed to.

        `None` leaves the SDK's own default (the client's configured
        `api_endpoint`, i.e. real GCS unless an emulator is configured).
        """
        if internal:
            return self._endpoint_url
        return self._public_endpoint_url or self._endpoint_url

    async def signed_url(
        self, path: str, expires_in: int = 900, write: bool = False, internal: bool = False
    ) -> str:
        if self._identity_type == "none":
            return self._anonymous_url(path, write=write, internal=internal)

        client = await self._get_client()
        blob = client.bucket(self._bucket_name).blob(path)

        kwargs: dict[str, Any] = {
            "version": "v4",
            "expiration": timedelta(seconds=expires_in),
            "method": "PUT" if write else "GET",
        }
        endpoint = self._access_endpoint(internal=internal)
        if endpoint:
            kwargs["api_access_endpoint"] = endpoint

        if self._identity_type == "managed_identity":
            credentials = self._credentials
            if credentials is None:
                raise ConnectorConfigError("gcs connector has no credentials to sign with")
            try:
                await asyncio.to_thread(credentials.refresh, GoogleAuthRequest())
            except RefreshError as exc:
                raise ConnectorAuthError(
                    f"could not refresh credentials to sign a url for {path!r}"
                ) from exc
            service_account_email = getattr(credentials, "service_account_email", None)
            if not service_account_email or service_account_email == "default":
                raise ConnectorConfigError(
                    "managed_identity identity could not determine a service account "
                    "email to sign with"
                )
            kwargs["service_account_email"] = service_account_email
            kwargs["access_token"] = credentials.token

        try:
            url: str = await asyncio.to_thread(blob.generate_signed_url, **kwargs)
            return url
        except _AUTH_ERRORS as exc:
            raise ConnectorAuthError(f"authentication failed signing url for {path!r}") from exc

    def _anonymous_url(self, path: str, *, write: bool, internal: bool) -> str:
        """The object's plain URL: anonymous credentials have no key to sign with.

        It reads from a public bucket or an emulator. Writes are refused: GCS
        has no anonymous upload URL, and the SDK would fail on the missing key.
        """
        if write:
            raise UnsupportedOperation(
                "a gcs connector with identity_type 'none' cannot sign upload URLs; "
                "use account_key or managed_identity"
            )
        endpoint = self._access_endpoint(internal=internal) or _DEFAULT_ACCESS_ENDPOINT
        return f"{endpoint}/{quote(self._bucket_name)}/{quote(path)}"

    async def check(self) -> ConnectorCheck:
        try:
            client = await self._get_client()
            bucket = client.bucket(self._bucket_name)
            await asyncio.to_thread(bucket.reload)
        except NotFound:
            return ConnectorCheck(ok=False, messages=[f"bucket not found: {self._bucket_name!r}"])
        except _AUTH_ERRORS as exc:
            return ConnectorCheck(
                ok=False, messages=[f"authentication failed: {exc.__class__.__name__}"]
            )
        except ConnectorConfigError as exc:
            return ConnectorCheck(ok=False, messages=[str(exc)])

        messages = [f"bucket {self._bucket_name!r} is reachable"]
        if self._frontend_origin:
            messages.append(self._check_cors(bucket))

        return ConnectorCheck(ok=True, messages=messages)

    def _check_cors(self, bucket: storage.Bucket) -> str:
        cors_rules = bucket.cors or []
        matching = [
            rule
            for rule in cors_rules
            if self._frontend_origin in rule.get("origin", []) or "*" in rule.get("origin", [])
        ]
        if not matching:
            return (
                f"CORS warning: frontend origin {self._frontend_origin!r} is not in the "
                "bucket's allowed origins (SRC-7)"
            )
        # Browser uploads PUT straight to the bucket (§12 upload path); a
        # read-only CORS rule makes every upload fail with an opaque network
        # error, so say so here rather than there.
        can_put = any(
            "PUT" in {method.upper() for method in rule.get("method", [])}
            or "*" in rule.get("method", [])
            for rule in matching
        )
        if can_put:
            return f"CORS allows frontend origin {self._frontend_origin!r} (GET and PUT)"
        return (
            f"CORS allows frontend origin {self._frontend_origin!r} for reads only; "
            "add PUT to the bucket's CORS configuration to allow browser uploads (SRC-7)"
        )


def build_gcs(config: dict[str, Any], secret: str | None = None) -> GCSConnector:
    """Construct a `GCSConnector` from a `connector` row's config dict.

    Mirrors `registry._build_azure_blob`: `identity_type` travels inside
    `config` (as every existing builder expects it), and only `bucket` is
    strictly required — `identity_type` defaults to `managed_identity`
    (Application Default Credentials), the zero-config option in a GCP
    deployment.
    """
    if "bucket" not in config:
        raise ConnectorConfigError("gcs connector config requires 'bucket'")
    return GCSConnector(
        bucket=config["bucket"],
        identity_type=config.get("identity_type", "managed_identity"),
        secret=secret,
        project=config.get("project"),
        endpoint_url=config.get("endpoint_url"),
        public_endpoint_url=config.get("public_endpoint_url"),
        frontend_origin=config.get("frontend_origin"),
    )
