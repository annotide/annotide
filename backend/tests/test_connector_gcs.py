"""Tests for `app.connectors.gcs`.

`GCSConnector` is tested for its pure logic (config validation, error
mapping, glob filtering, CORS check) with the SDK client mocked via
`unittest.mock.MagicMock` run through `asyncio.to_thread`, plus one set of
tests that generate a throwaway RSA service-account key and let V4 URL
signing run for real, fully offline (no network call is needed to *sign* a
URL — only to actually use it). No real network calls are made anywhere in
this file.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.api_core.exceptions import Forbidden, NotFound, Unauthorized
from google.auth.credentials import AnonymousCredentials

from app.connectors.base import ObjectInfo
from app.connectors.errors import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorNotFound,
    UnsupportedOperation,
)
from app.connectors.gcs import GCSConnector, build_gcs


def _service_account_info(
    email: str = "svc@test-project.iam.gserviceaccount.com",
) -> dict[str, str]:
    """A syntactically valid, throwaway service-account key.

    The private key is real (generated on the fly) so
    `service_account.Credentials.from_service_account_info` accepts it and
    `Blob.generate_signed_url` can actually RSA-sign a URL with it — no
    network call is involved in signing, only in eventually fetching it.
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return {
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "key-id-123",
        "private_key": pem,
        "client_email": email,
        "client_id": "123456789",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


def _blob(
    name: str,
    *,
    size: int = 3,
    etag: str | None = '"abc"',
    updated: datetime | None = None,
    content_type: str | None = "image/jpeg",
) -> MagicMock:
    blob = MagicMock()
    blob.name = name
    blob.size = size
    blob.etag = etag
    blob.updated = updated
    blob.content_type = content_type
    return blob


async def _connector_with_fake_client(connector: GCSConnector, client: MagicMock) -> GCSConnector:
    connector._get_client = AsyncMock(return_value=client)  # type: ignore[method-assign]
    return connector


# --------------------------------------------------------------------------
# Credential / config validation
# --------------------------------------------------------------------------


class TestBuildCredentials:
    def test_account_key_requires_a_secret(self) -> None:
        connector = GCSConnector(bucket="b", identity_type="account_key")
        with pytest.raises(ConnectorConfigError, match="requires a secret"):
            connector._build_credentials()

    def test_account_key_rejects_malformed_json(self) -> None:
        connector = GCSConnector(bucket="b", identity_type="account_key", secret="not json")
        with pytest.raises(ConnectorConfigError, match="not valid JSON"):
            connector._build_credentials()

    def test_account_key_rejects_incomplete_key(self) -> None:
        connector = GCSConnector(
            bucket="b", identity_type="account_key", secret=json.dumps({"client_email": "x"})
        )
        with pytest.raises(ConnectorConfigError, match="not a valid service-account key"):
            connector._build_credentials()

    def test_account_key_accepts_a_real_key(self) -> None:
        info = _service_account_info()
        connector = GCSConnector(bucket="b", identity_type="account_key", secret=json.dumps(info))
        credentials = connector._build_credentials()
        assert credentials.service_account_email == info["client_email"]

    def test_none_identity_is_anonymous(self) -> None:
        connector = GCSConnector(bucket="b", identity_type="none")
        assert isinstance(connector._build_credentials(), AnonymousCredentials)

    def test_unsupported_identity_type(self) -> None:
        connector = GCSConnector(bucket="b", identity_type="service_principal")
        with pytest.raises(ConnectorConfigError, match="unsupported identity_type"):
            connector._build_credentials()


# --------------------------------------------------------------------------
# list()
# --------------------------------------------------------------------------


class TestList:
    async def test_list_all(self) -> None:
        client = MagicMock()
        client.list_blobs.return_value = [_blob("a.jpg"), _blob("images/b.png")]
        connector = await _connector_with_fake_client(
            GCSConnector(bucket="b", identity_type="none"), client
        )
        results = [obj async for obj in connector.list("")]
        assert [o.path for o in results] == ["a.jpg", "images/b.png"]
        client.list_blobs.assert_called_once_with("b", prefix=None)

    async def test_list_with_prefix_normalises(self) -> None:
        client = MagicMock()
        client.list_blobs.return_value = [_blob("images/a.jpg")]
        connector = await _connector_with_fake_client(
            GCSConnector(bucket="b", identity_type="none"), client
        )
        results = [obj async for obj in connector.list("images/")]
        assert [o.path for o in results] == ["images/a.jpg"]
        client.list_blobs.assert_called_once_with("b", prefix="images")

    async def test_list_with_glob(self) -> None:
        client = MagicMock()
        client.list_blobs.return_value = [_blob("a.jpg"), _blob("b.png")]
        connector = await _connector_with_fake_client(
            GCSConnector(bucket="b", identity_type="none"), client
        )
        results = [obj async for obj in connector.list("", glob="*.jpg")]
        assert [o.path for o in results] == ["a.jpg"]

    async def test_list_maps_object_info_fields(self) -> None:
        now = datetime(2024, 1, 1)
        client = MagicMock()
        client.list_blobs.return_value = [
            _blob("a.jpg", size=42, etag='"xyz"', updated=now, content_type="image/jpeg")
        ]
        connector = await _connector_with_fake_client(
            GCSConnector(bucket="b", identity_type="none"), client
        )
        [info] = [obj async for obj in connector.list("")]
        assert info == ObjectInfo(
            path="a.jpg", size_bytes=42, etag="xyz", last_modified=now, content_type="image/jpeg"
        )


# --------------------------------------------------------------------------
# read()
# --------------------------------------------------------------------------


class TestRead:
    async def _connector(self, blob: MagicMock) -> GCSConnector:
        bucket = MagicMock()
        bucket.blob.return_value = blob
        client = MagicMock()
        client.bucket.return_value = bucket
        return await _connector_with_fake_client(
            GCSConnector(bucket="b", identity_type="none"), client
        )

    async def test_read_full(self) -> None:
        blob = MagicMock()
        blob.download_as_bytes.return_value = b"0123456789"
        connector = await self._connector(blob)
        assert await connector.read("f.bin") == b"0123456789"
        blob.download_as_bytes.assert_called_once_with(start=None, end=None)

    async def test_read_range_end_is_exclusive_like_the_interface(self) -> None:
        """Interface `end` is exclusive; GCS's own `end` is inclusive."""
        blob = MagicMock()
        blob.download_as_bytes.return_value = b"234"
        connector = await self._connector(blob)
        assert await connector.read("f.bin", start=2, end=5) == b"234"
        blob.download_as_bytes.assert_called_once_with(start=2, end=4)

    async def test_read_start_only(self) -> None:
        blob = MagicMock()
        blob.download_as_bytes.return_value = b"789"
        connector = await self._connector(blob)
        assert await connector.read("f.bin", start=7) == b"789"
        blob.download_as_bytes.assert_called_once_with(start=7, end=None)

    async def test_read_missing_raises(self) -> None:
        blob = MagicMock()
        blob.download_as_bytes.side_effect = NotFound("nope")
        connector = await self._connector(blob)
        with pytest.raises(ConnectorNotFound):
            await connector.read("missing.bin")

    async def test_read_forbidden_raises_auth_error(self) -> None:
        blob = MagicMock()
        blob.download_as_bytes.side_effect = Forbidden("no")
        connector = await self._connector(blob)
        with pytest.raises(ConnectorAuthError):
            await connector.read("f.bin")

    async def test_read_unauthorized_raises_auth_error(self) -> None:
        blob = MagicMock()
        blob.download_as_bytes.side_effect = Unauthorized("no")
        connector = await self._connector(blob)
        with pytest.raises(ConnectorAuthError):
            await connector.read("f.bin")


# --------------------------------------------------------------------------
# write() / delete()
# --------------------------------------------------------------------------


class TestWrite:
    async def test_write_uploads_with_content_type(self) -> None:
        blob = MagicMock()
        bucket = MagicMock()
        bucket.blob.return_value = blob
        client = MagicMock()
        client.bucket.return_value = bucket
        connector = await _connector_with_fake_client(
            GCSConnector(bucket="b", identity_type="none"), client
        )
        await connector.write("f.txt", b"payload", "text/plain")
        blob.upload_from_string.assert_called_once_with(b"payload", content_type="text/plain")

    async def test_write_forbidden_raises_auth_error(self) -> None:
        blob = MagicMock()
        blob.upload_from_string.side_effect = Forbidden("no")
        bucket = MagicMock()
        bucket.blob.return_value = blob
        client = MagicMock()
        client.bucket.return_value = bucket
        connector = await _connector_with_fake_client(
            GCSConnector(bucket="b", identity_type="none"), client
        )
        with pytest.raises(ConnectorAuthError):
            await connector.write("f.txt", b"data", "text/plain")


class TestDelete:
    async def test_delete(self) -> None:
        blob = MagicMock()
        bucket = MagicMock()
        bucket.blob.return_value = blob
        client = MagicMock()
        client.bucket.return_value = bucket
        connector = await _connector_with_fake_client(
            GCSConnector(bucket="b", identity_type="none"), client
        )
        await connector.delete("f.txt")
        blob.delete.assert_called_once_with()

    async def test_delete_missing_raises(self) -> None:
        blob = MagicMock()
        blob.delete.side_effect = NotFound("nope")
        bucket = MagicMock()
        bucket.blob.return_value = blob
        client = MagicMock()
        client.bucket.return_value = bucket
        connector = await _connector_with_fake_client(
            GCSConnector(bucket="b", identity_type="none"), client
        )
        with pytest.raises(ConnectorNotFound):
            await connector.delete("missing.txt")

    async def test_delete_auth_error(self) -> None:
        blob = MagicMock()
        blob.delete.side_effect = Unauthorized("no")
        bucket = MagicMock()
        bucket.blob.return_value = blob
        client = MagicMock()
        client.bucket.return_value = bucket
        connector = await _connector_with_fake_client(
            GCSConnector(bucket="b", identity_type="none"), client
        )
        with pytest.raises(ConnectorAuthError):
            await connector.delete("f.txt")


# --------------------------------------------------------------------------
# check()
# --------------------------------------------------------------------------


class TestCheck:
    async def _connector(self, bucket: MagicMock, **kwargs: Any) -> GCSConnector:
        client = MagicMock()
        client.bucket.return_value = bucket
        return await _connector_with_fake_client(
            GCSConnector(bucket="b", identity_type="none", **kwargs), client
        )

    async def test_check_ok_without_cors(self) -> None:
        bucket = MagicMock()
        connector = await self._connector(bucket)
        result = await connector.check()
        assert result.ok
        assert "reachable" in result.messages[0]

    async def test_check_not_found(self) -> None:
        bucket = MagicMock()
        bucket.reload.side_effect = NotFound("nope")
        connector = await self._connector(bucket)
        result = await connector.check()
        assert not result.ok
        assert "not found" in result.messages[0]

    async def test_check_auth_error(self) -> None:
        bucket = MagicMock()
        bucket.reload.side_effect = Forbidden("no")
        connector = await self._connector(bucket)
        result = await connector.check()
        assert not result.ok
        assert "authentication failed" in result.messages[0]

    async def test_check_config_error(self) -> None:
        connector = GCSConnector(bucket="b", identity_type="account_key")  # no secret
        result = await connector.check()
        assert not result.ok
        assert "requires a secret" in result.messages[0]

    async def test_check_cors_warns_when_origin_missing(self) -> None:
        bucket = MagicMock()
        bucket.cors = []
        connector = await self._connector(bucket, frontend_origin="https://app.example.com")
        result = await connector.check()
        assert result.ok
        assert "CORS warning" in result.messages[1]

    async def test_check_cors_ok_with_put(self) -> None:
        bucket = MagicMock()
        bucket.cors = [{"origin": ["https://app.example.com"], "method": ["GET", "PUT"]}]
        connector = await self._connector(bucket, frontend_origin="https://app.example.com")
        result = await connector.check()
        assert "GET and PUT" in result.messages[1]

    async def test_check_cors_warns_read_only(self) -> None:
        bucket = MagicMock()
        bucket.cors = [{"origin": ["https://app.example.com"], "method": ["GET"]}]
        connector = await self._connector(bucket, frontend_origin="https://app.example.com")
        result = await connector.check()
        assert "reads only" in result.messages[1]


# --------------------------------------------------------------------------
# signed_url() — real V4 signing against a throwaway key, offline.
# --------------------------------------------------------------------------


class TestSignedUrl:
    async def test_get_url_shape(self) -> None:
        info = _service_account_info()
        connector = GCSConnector(
            bucket="acme-media", identity_type="account_key", secret=json.dumps(info)
        )
        url = await connector.signed_url("images/cat.jpg", expires_in=600)

        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        assert "acme-media" in parsed.path or "acme-media" in (parsed.netloc)
        assert "images/cat.jpg" in parsed.path
        assert query["X-Goog-Algorithm"] == ["GOOG4-RSA-SHA256"]
        assert query["X-Goog-Expires"] == ["600"]
        assert "X-Goog-Signature" in query

    async def test_write_uses_put_method(self) -> None:
        info = _service_account_info()
        connector = GCSConnector(
            bucket="acme-media", identity_type="account_key", secret=json.dumps(info)
        )
        get_url = await connector.signed_url("cat.jpg")
        put_url = await connector.signed_url("cat.jpg", write=True)
        assert (
            parse_qs(urlparse(get_url).query)["X-Goog-Signature"]
            != parse_qs(urlparse(put_url).query)["X-Goog-Signature"]
        )

    async def test_internal_uses_endpoint_url_host(self) -> None:
        info = _service_account_info()
        connector = GCSConnector(
            bucket="acme-media",
            identity_type="account_key",
            secret=json.dumps(info),
            endpoint_url="http://gcs-emulator:4443",
            public_endpoint_url="http://localhost:4443",
        )
        url = await connector.signed_url("cat.jpg", internal=True)
        assert url.startswith("http://gcs-emulator:4443/")

    async def test_public_uses_public_endpoint_url_host(self) -> None:
        info = _service_account_info()
        connector = GCSConnector(
            bucket="acme-media",
            identity_type="account_key",
            secret=json.dumps(info),
            endpoint_url="http://gcs-emulator:4443",
            public_endpoint_url="http://localhost:4443",
        )
        url = await connector.signed_url("cat.jpg")
        assert url.startswith("http://localhost:4443/")
        assert "gcs-emulator" not in url, "a browser cannot resolve the compose service name"

    async def test_public_defaults_to_endpoint_url_when_unset(self) -> None:
        info = _service_account_info()
        connector = GCSConnector(
            bucket="acme-media",
            identity_type="account_key",
            secret=json.dumps(info),
            endpoint_url="http://gcs-emulator:4443",
        )
        url = await connector.signed_url("cat.jpg")
        assert url.startswith("http://gcs-emulator:4443/")

    async def test_defaults_to_real_gcs_host_without_an_emulator(self) -> None:
        info = _service_account_info()
        connector = GCSConnector(
            bucket="acme-media", identity_type="account_key", secret=json.dumps(info)
        )
        url = await connector.signed_url("cat.jpg")
        assert "storage.googleapis.com" in url

    async def test_managed_identity_signs_via_iam_signblob(self) -> None:
        credentials = MagicMock()
        credentials.token = "access-token-abc"
        credentials.service_account_email = "robot@test-project.iam.gserviceaccount.com"
        credentials.refresh = MagicMock()

        blob = MagicMock()
        blob.generate_signed_url.return_value = "https://storage.googleapis.com/signed"
        bucket = MagicMock()
        bucket.blob.return_value = blob
        client = MagicMock()
        client.bucket.return_value = bucket

        connector = GCSConnector(bucket="b", identity_type="managed_identity")
        connector._get_client = AsyncMock(return_value=client)  # type: ignore[method-assign]
        connector._credentials = credentials

        url = await connector.signed_url("cat.jpg")

        assert url == "https://storage.googleapis.com/signed"
        credentials.refresh.assert_called_once()
        _, kwargs = blob.generate_signed_url.call_args
        assert kwargs["service_account_email"] == "robot@test-project.iam.gserviceaccount.com"
        assert kwargs["access_token"] == "access-token-abc"

    async def test_managed_identity_without_email_is_a_config_error(self) -> None:
        credentials = MagicMock()
        credentials.service_account_email = "default"
        credentials.refresh = MagicMock()

        client = MagicMock()
        connector = GCSConnector(bucket="b", identity_type="managed_identity")
        connector._get_client = AsyncMock(return_value=client)  # type: ignore[method-assign]
        connector._credentials = credentials

        with pytest.raises(ConnectorConfigError, match="service account email"):
            await connector.signed_url("cat.jpg")

    async def test_anonymous_get_is_the_plain_object_url(self) -> None:
        connector = GCSConnector(bucket="acme media", identity_type="none")
        assert await connector.signed_url("a b/cat.jpg") == (
            "https://storage.googleapis.com/acme%20media/a%20b/cat.jpg"
        )

    async def test_anonymous_url_uses_the_public_then_internal_endpoint(self) -> None:
        connector = GCSConnector(
            bucket="demo",
            identity_type="none",
            endpoint_url="http://gcs:4443/",
            public_endpoint_url="http://localhost:4443",
        )
        assert await connector.signed_url("cat.jpg") == "http://localhost:4443/demo/cat.jpg"
        assert (
            await connector.signed_url("cat.jpg", internal=True) == "http://gcs:4443/demo/cat.jpg"
        )

    async def test_anonymous_upload_url_is_unsupported(self) -> None:
        connector = GCSConnector(bucket="demo", identity_type="none")
        with pytest.raises(UnsupportedOperation, match="cannot sign upload URLs"):
            await connector.signed_url("cat.jpg", write=True)


# --------------------------------------------------------------------------
# build_gcs()
# --------------------------------------------------------------------------


class TestBuildGCS:
    def test_missing_bucket_raises(self) -> None:
        with pytest.raises(ConnectorConfigError, match="requires 'bucket'"):
            build_gcs({})

    def test_builds_with_defaults(self) -> None:
        connector = build_gcs({"bucket": "acme-media"})
        assert isinstance(connector, GCSConnector)
        assert connector._bucket_name == "acme-media"
        assert connector._identity_type == "managed_identity"

    def test_passes_through_optional_keys(self) -> None:
        connector = build_gcs(
            {
                "bucket": "acme-media",
                "identity_type": "account_key",
                "project": "acme-prod",
                "endpoint_url": "http://gcs-emulator:4443",
                "public_endpoint_url": "http://localhost:4443",
                "frontend_origin": "https://app.example.com",
            },
            secret="{}",
        )
        assert connector._identity_type == "account_key"
        assert connector._project == "acme-prod"
        assert connector._endpoint_url == "http://gcs-emulator:4443"
        assert connector._public_endpoint_url == "http://localhost:4443"
        assert connector._frontend_origin == "https://app.example.com"
        assert connector._secret == "{}"
