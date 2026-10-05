"""AWS S3 (and S3-compatible: MinIO, etc.) storage connector (`connector_type = "s3"`).

Supports the `identity_type` values relevant to S3: ``access_key`` (a static
key pair), ``iam_role`` (the default AWS credential chain, optionally
assuming ``role_arn`` via STS) and ``none`` (anonymous access to a public
bucket, tests only). Secrets arrive as already-resolved plain strings — the
caller is responsible for resolving a connector's ``secret_ref`` first. For
``access_key`` the resolved secret is JSON: ``{"access_key_id",
"secret_access_key"}``.

Uses ``aioboto3`` so every operation is a real ``await``, no thread pool.
``aioboto3``/``aiobotocore``/``botocore`` ship no inline type stubs (mypy
``strict`` flags each import); each import is narrowly ignored below rather
than typed via ``[[tool.mypy.overrides]]`` — the caller of this module wires
that in ``pyproject.toml``, which this module must not touch.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, NoReturn
from urllib.parse import quote

import aioboto3  # type: ignore[import-untyped]  # no inline type stubs shipped
from botocore import UNSIGNED  # type: ignore[import-untyped]  # no inline type stubs shipped
from botocore.config import Config  # type: ignore[import-untyped]  # no inline type stubs shipped
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from app.connectors.base import BaseStorageConnector, ConnectorCheck, ObjectInfo
from app.connectors.errors import ConnectorAuthError, ConnectorConfigError, ConnectorNotFound

#: Identity types this connector accepts (the S3-relevant subset of the
#: contract's `connector_identity` enum).
SUPPORTED_IDENTITY_TYPES = frozenset({"access_key", "iam_role", "none"})

#: Refresh assumed-role credentials this many seconds before they actually
#: expire, so a request is never signed with a credential that expires
#: mid-flight.
_ROLE_CREDENTIALS_SAFETY_MARGIN_SECONDS = 60

#: `RoleSessionName` for the STS `AssumeRole` call. STS requires a name;
#: it shows up in the assumed role's CloudTrail entries.
_ROLE_SESSION_NAME = "annotide"

#: Error codes for objects that do not exist, from either `HeadObject` or
#: `GetObject`/`DeleteObject`; S3 is inconsistent about which one it uses.
_NOT_FOUND_CODES = frozenset({"NoSuchKey", "404", "NotFound"})

#: Error codes for a request the caller's credentials cannot make.
_AUTH_ERROR_CODES = frozenset(
    {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "403", "Forbidden"}
)


class S3Connector(BaseStorageConnector):
    """Connector backed by an S3 (or S3-compatible) bucket."""

    type: ClassVar[str] = "s3"

    def __init__(
        self,
        bucket: str,
        identity_type: str,
        *,
        secret: str | None = None,
        region: str | None = None,
        endpoint_url: str | None = None,
        public_endpoint_url: str | None = None,
        force_path_style: bool = False,
        frontend_origin: str | None = None,
        role_arn: str | None = None,
        page_size: int | None = None,
    ) -> None:
        self._bucket = bucket
        self._identity_type = identity_type
        self._secret = secret
        self._region = region
        self._endpoint_url = endpoint_url.rstrip("/") if endpoint_url else None
        # The endpoint a browser must use. It differs from `endpoint_url`
        # whenever the API reaches storage over a private name the browser
        # cannot resolve — a compose service name, a MinIO container. A
        # signed URL built from the internal name is fetched by the browser,
        # not the API, so it simply fails to load with no server-side error.
        self._public_endpoint_url = public_endpoint_url.rstrip("/") if public_endpoint_url else None
        self._force_path_style = force_path_style
        self._frontend_origin = frontend_origin
        self._role_arn = role_arn
        self._page_size = page_size

        self._session = aioboto3.Session()
        self._client: Any | None = None
        self._client_ctx: Any | None = None
        self._signing_client: Any | None = None
        self._signing_client_ctx: Any | None = None

        self._role_credentials: dict[str, str] | None = None
        self._role_credentials_expiry: datetime | None = None

    def _config(self) -> Any:
        addressing_style = "path" if self._force_path_style else "auto"
        if self._identity_type == "none":
            return Config(s3={"addressing_style": addressing_style}, signature_version=UNSIGNED)
        return Config(s3={"addressing_style": addressing_style}, signature_version="s3v4")

    async def _assume_role_credentials(self) -> dict[str, str]:
        """Assume `role_arn` via STS, caching the result until near expiry."""
        now = datetime.now(UTC)
        margin = timedelta(seconds=_ROLE_CREDENTIALS_SAFETY_MARGIN_SECONDS)
        if (
            self._role_credentials is not None
            and self._role_credentials_expiry is not None
            and self._role_credentials_expiry - margin > now
        ):
            return self._role_credentials

        async with self._session.client(
            "sts", region_name=self._region, endpoint_url=self._endpoint_url
        ) as sts:
            response = await sts.assume_role(
                RoleArn=self._role_arn, RoleSessionName=_ROLE_SESSION_NAME
            )
        creds = response["Credentials"]
        self._role_credentials = {
            "aws_access_key_id": creds["AccessKeyId"],
            "aws_secret_access_key": creds["SecretAccessKey"],
            "aws_session_token": creds["SessionToken"],
        }
        self._role_credentials_expiry = creds["Expiration"]
        return self._role_credentials

    async def _credential_kwargs(self) -> dict[str, str]:
        """Resolve credential keyword arguments for `session.client(...)`.

        Raises :class:`ConnectorConfigError` for a missing or malformed
        secret. Returns an empty dict for `iam_role` without `role_arn`
        (the default AWS credential chain) and for `none` (anonymous).
        """
        if self._identity_type == "access_key":
            if not self._secret:
                raise ConnectorConfigError("access_key identity requires a secret")
            try:
                parsed = json.loads(self._secret)
                access_key_id = parsed["access_key_id"]
                secret_access_key = parsed["secret_access_key"]
            except (json.JSONDecodeError, TypeError, KeyError) as exc:
                raise ConnectorConfigError(
                    "access_key identity requires a JSON secret with "
                    "'access_key_id' and 'secret_access_key'"
                ) from exc
            return {
                "aws_access_key_id": access_key_id,
                "aws_secret_access_key": secret_access_key,
            }

        if self._identity_type == "iam_role":
            if self._role_arn:
                return await self._assume_role_credentials()
            return {}

        if self._identity_type == "none":
            return {}

        raise ConnectorConfigError(f"unsupported identity_type for s3: {self._identity_type!r}")

    async def _create_client(self, *, endpoint_url: str | None) -> Any:
        credential_kwargs = await self._credential_kwargs()
        ctx = self._session.client(
            "s3",
            region_name=self._region,
            endpoint_url=endpoint_url,
            config=self._config(),
            **credential_kwargs,
        )
        client = await ctx.__aenter__()
        return ctx, client

    async def _get_client(self) -> Any:
        if self._client is None:
            self._client_ctx, self._client = await self._create_client(
                endpoint_url=self._endpoint_url
            )
        return self._client

    async def _get_signing_client(self, *, internal: bool) -> Any:
        target = (
            self._endpoint_url if internal else (self._public_endpoint_url or self._endpoint_url)
        )
        if target == self._endpoint_url:
            return await self._get_client()
        if self._signing_client is None:
            self._signing_client_ctx, self._signing_client = await self._create_client(
                endpoint_url=target
            )
        return self._signing_client

    def _object_url(self, path: str, *, public: bool = False) -> str:
        """The object's plain URL, used by the `none` identity (unsigned)."""
        configured = self._public_endpoint_url if public else self._endpoint_url
        quoted = quote(path, safe="/")
        if configured:
            return f"{configured}/{self._bucket}/{quoted}"
        region = self._region or "us-east-1"
        if self._force_path_style:
            return f"https://s3.{region}.amazonaws.com/{self._bucket}/{quoted}"
        return f"https://{self._bucket}.s3.{region}.amazonaws.com/{quoted}"

    async def aclose(self) -> None:
        """Release the SDK clients.

        Each holds an aiohttp session. A connector is built per request, so
        without this every item view leaks a session and its connection pool
        until the process is restarted.
        """
        for ctx_attr, client_attr in (
            ("_client_ctx", "_client"),
            ("_signing_client_ctx", "_signing_client"),
        ):
            ctx = getattr(self, ctx_attr)
            if ctx is not None:
                with contextlib.suppress(Exception):
                    await ctx.__aexit__(None, None, None)
                setattr(self, ctx_attr, None)
                setattr(self, client_attr, None)

    async def list(self, prefix: str, glob: str | None = None) -> AsyncIterator[ObjectInfo]:
        client = await self._get_client()
        normalised = self._normalise_prefix(prefix)
        pagination_config = {"PageSize": self._page_size} if self._page_size else {}
        paginator = client.get_paginator("list_objects_v2")
        async for page in paginator.paginate(
            Bucket=self._bucket, Prefix=normalised, PaginationConfig=pagination_config
        ):
            for obj in page.get("Contents", []):
                path = obj["Key"]
                if not self._matches_prefix_and_glob(path, prefix, glob):
                    continue
                etag = obj.get("ETag")
                yield ObjectInfo(
                    path=path,
                    size_bytes=obj.get("Size", 0),
                    etag=etag.strip('"') if etag else None,
                    last_modified=obj.get("LastModified"),
                    # ListObjectsV2 does not return content-type; only
                    # HeadObject/GetObject do, and fetching it per listed
                    # object would turn a single list call into N.
                    content_type=None,
                )

    async def read(self, path: str, start: int | None = None, end: int | None = None) -> bytes:
        client = await self._get_client()
        kwargs: dict[str, Any] = {"Bucket": self._bucket, "Key": path}
        if start is not None or end is not None:
            offset = start or 0
            kwargs["Range"] = f"bytes={offset}-{end - 1}" if end is not None else f"bytes={offset}-"
        try:
            response = await client.get_object(**kwargs)
            body = await response["Body"].read()
            return bytes(body)
        except ClientError as exc:
            self._reraise_mapped(exc, f"object not found: {path!r}", f"object {path!r}")

    async def write(self, path: str, data: bytes, content_type: str) -> None:
        client = await self._get_client()
        try:
            await client.put_object(
                Bucket=self._bucket, Key=path, Body=data, ContentType=content_type
            )
        except ClientError as exc:
            self._reraise_mapped(exc, f"object not found: {path!r}", f"writing object {path!r}")

    async def delete(self, path: str) -> None:
        client = await self._get_client()
        try:
            await client.head_object(Bucket=self._bucket, Key=path)
        except ClientError as exc:
            self._reraise_mapped(exc, f"object not found: {path!r}", f"object {path!r}")
        try:
            await client.delete_object(Bucket=self._bucket, Key=path)
        except ClientError as exc:
            self._reraise_mapped(exc, f"object not found: {path!r}", f"deleting object {path!r}")

    @staticmethod
    def _reraise_mapped(exc: Any, not_found_message: str, auth_message_subject: str) -> NoReturn:
        """Map a `ClientError` to the shared connector exception hierarchy."""
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in _NOT_FOUND_CODES:
            raise ConnectorNotFound(not_found_message) from exc
        if code in _AUTH_ERROR_CODES:
            raise ConnectorAuthError(f"authentication failed for {auth_message_subject}") from exc
        raise exc

    async def signed_url(
        self, path: str, expires_in: int = 900, write: bool = False, internal: bool = False
    ) -> str:
        if self._identity_type == "none":
            # No credentials to sign with; the bucket must already allow
            # anonymous access, so the plain URL is all a browser needs.
            return self._object_url(path, public=not internal)

        client = await self._get_signing_client(internal=internal)
        operation = "put_object" if write else "get_object"
        url: str = await client.generate_presigned_url(
            operation,
            Params={"Bucket": self._bucket, "Key": path},
            ExpiresIn=expires_in,
        )
        return url

    async def check(self) -> ConnectorCheck:
        try:
            client = await self._get_client()
            await client.head_bucket(Bucket=self._bucket)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in _NOT_FOUND_CODES:
                return ConnectorCheck(ok=False, messages=[f"bucket not found: {self._bucket!r}"])
            if code in _AUTH_ERROR_CODES:
                return ConnectorCheck(
                    ok=False, messages=[f"authentication failed: {exc.__class__.__name__}"]
                )
            return ConnectorCheck(ok=False, messages=[str(exc)])
        except ConnectorConfigError as exc:
            return ConnectorCheck(ok=False, messages=[str(exc)])

        messages = [f"bucket {self._bucket!r} is reachable"]

        if self._frontend_origin:
            messages.append(await self._check_cors(client))

        return ConnectorCheck(ok=True, messages=messages)

    async def _check_cors(self, client: Any) -> str:
        try:
            response = await client.get_bucket_cors(Bucket=self._bucket)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code == "NoSuchCORSConfiguration":
                return (
                    f"CORS warning: bucket {self._bucket!r} has no CORS rules; "
                    f"frontend origin {self._frontend_origin!r} cannot read or write it (SRC-7)"
                )
            return (
                "could not read CORS rules with the current identity; "
                "verify the bucket's CORS configuration manually (SRC-7)"
            )

        rules = response.get("CORSRules", [])
        matching = [
            rule
            for rule in rules
            if self._frontend_origin in rule.get("AllowedOrigins", [])
            or "*" in rule.get("AllowedOrigins", [])
        ]
        if not matching:
            return (
                f"CORS warning: frontend origin {self._frontend_origin!r} is not in the "
                "bucket's allowed origins (SRC-7)"
            )
        can_put = any(
            "PUT" in {method.upper() for method in rule.get("AllowedMethods", [])}
            for rule in matching
        )
        if can_put:
            return f"CORS allows frontend origin {self._frontend_origin!r} (GET and PUT)"
        return (
            f"CORS allows frontend origin {self._frontend_origin!r} for reads only; "
            "add PUT to allow browser uploads (SRC-7)"
        )


def build_s3(config: dict[str, Any], secret: str | None = None) -> S3Connector:
    """Construct an `S3Connector` from a `connector` row's config dict.

    Only `bucket` is required; every other key has a working default
    (`identity_type` defaults to `iam_role`, the default AWS credential
    chain).
    """
    missing = [key for key in ("bucket",) if key not in config]
    if missing:
        raise ConnectorConfigError(
            f"s3 connector config missing required key(s): {', '.join(missing)}"
        )
    return S3Connector(
        bucket=config["bucket"],
        identity_type=config.get("identity_type", "iam_role"),
        secret=secret,
        region=config.get("region"),
        endpoint_url=config.get("endpoint_url"),
        public_endpoint_url=config.get("public_endpoint_url"),
        force_path_style=bool(config.get("force_path_style", False)),
        frontend_origin=config.get("frontend_origin"),
        role_arn=config.get("role_arn"),
    )
