"""Tests for `app.connectors`.

`LocalConnector` is tested end-to-end against `tmp_path` (real filesystem,
no mocking). `AzureBlobConnector` is tested for its pure logic only —
credential-factory branching, glob filtering, and that `check()` /
`signed_url()` are wired correctly — with the Azure SDK client mocked via
`unittest.mock.AsyncMock`/`MagicMock`. No real network calls are made.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest
from azure.core.credentials import AzureSasCredential
from azure.core.exceptions import ClientAuthenticationError, ResourceNotFoundError
from azure.identity.aio import (
    ClientSecretCredential,
    DefaultAzureCredential,
    ManagedIdentityCredential,
)

from app.connectors import (
    AzureBlobConnector,
    ConnectorConfigError,
    ConnectorError,
    ConnectorNotFound,
    GCSConnector,
    HTTPConnector,
    LocalConnector,
    S3Connector,
    build_connector,
    get_connector_class,
)
from app.core.security import verify_storage_path

# --------------------------------------------------------------------------
# LocalConnector
# --------------------------------------------------------------------------


class TestLocalConnectorList:
    async def test_list_all_recursive(self, tmp_path: Any) -> None:
        (tmp_path / "a.txt").write_bytes(b"a")
        images = tmp_path / "images"
        images.mkdir()
        (images / "b.jpg").write_bytes(b"b")
        (images / "nested").mkdir()
        (images / "nested" / "c.png").write_bytes(b"c")

        connector = LocalConnector(root=tmp_path)
        results = [obj async for obj in connector.list("")]
        paths = sorted(o.path for o in results)
        assert paths == ["a.txt", "images/b.jpg", "images/nested/c.png"]

    async def test_list_with_prefix(self, tmp_path: Any) -> None:
        (tmp_path / "a.txt").write_bytes(b"a")
        images = tmp_path / "images"
        images.mkdir()
        (images / "b.jpg").write_bytes(b"b")

        connector = LocalConnector(root=tmp_path)
        results = [obj async for obj in connector.list("images")]
        assert [o.path for o in results] == ["images/b.jpg"]

    async def test_list_prefix_does_not_match_partial_directory_name(self, tmp_path: Any) -> None:
        images = tmp_path / "images"
        images.mkdir()
        (images / "a.jpg").write_bytes(b"a")
        imagesarchive = tmp_path / "imagesarchive"
        imagesarchive.mkdir()
        (imagesarchive / "b.jpg").write_bytes(b"b")

        connector = LocalConnector(root=tmp_path)
        results = [obj async for obj in connector.list("images")]
        assert [o.path for o in results] == ["images/a.jpg"]

    async def test_list_with_glob(self, tmp_path: Any) -> None:
        images = tmp_path / "images"
        images.mkdir()
        (images / "a.jpg").write_bytes(b"a")
        (images / "b.png").write_bytes(b"b")

        connector = LocalConnector(root=tmp_path)
        results = [obj async for obj in connector.list("", glob="*.jpg")]
        assert [o.path for o in results] == ["images/a.jpg"]

    async def test_list_empty_root(self, tmp_path: Any) -> None:
        connector = LocalConnector(root=tmp_path)
        results = [obj async for obj in connector.list("")]
        assert results == []

    async def test_list_etag_and_size(self, tmp_path: Any) -> None:
        (tmp_path / "x.txt").write_bytes(b"hello")

        connector = LocalConnector(root=tmp_path)
        [info] = [obj async for obj in connector.list("")]
        assert info.size_bytes == 5
        assert info.etag == hashlib.sha256(b"hello").hexdigest()
        assert info.last_modified is not None


class TestLocalConnectorRead:
    async def test_read_full(self, tmp_path: Any) -> None:
        (tmp_path / "f.bin").write_bytes(b"0123456789")
        connector = LocalConnector(root=tmp_path)
        assert await connector.read("f.bin") == b"0123456789"

    async def test_read_range(self, tmp_path: Any) -> None:
        (tmp_path / "f.bin").write_bytes(b"0123456789")
        connector = LocalConnector(root=tmp_path)
        assert await connector.read("f.bin", start=2, end=5) == b"234"

    async def test_read_start_only(self, tmp_path: Any) -> None:
        (tmp_path / "f.bin").write_bytes(b"0123456789")
        connector = LocalConnector(root=tmp_path)
        assert await connector.read("f.bin", start=7) == b"789"

    async def test_read_missing_raises(self, tmp_path: Any) -> None:
        connector = LocalConnector(root=tmp_path)
        with pytest.raises(ConnectorNotFound):
            await connector.read("missing.bin")


class TestLocalConnectorWrite:
    async def test_write_creates_parents_and_content(self, tmp_path: Any) -> None:
        connector = LocalConnector(root=tmp_path)
        await connector.write("nested/dir/file.txt", b"payload", "text/plain")
        assert (tmp_path / "nested" / "dir" / "file.txt").read_bytes() == b"payload"

    async def test_write_leaves_no_temp_files(self, tmp_path: Any) -> None:
        connector = LocalConnector(root=tmp_path)
        await connector.write("f.txt", b"data", "text/plain")
        assert os.listdir(tmp_path) == ["f.txt"]

    async def test_write_overwrite_replaces_content_atomically(self, tmp_path: Any) -> None:
        connector = LocalConnector(root=tmp_path)
        await connector.write("f.txt", b"first", "text/plain")
        await connector.write("f.txt", b"second", "text/plain")
        assert (tmp_path / "f.txt").read_bytes() == b"second"
        assert os.listdir(tmp_path) == ["f.txt"]


class TestLocalConnectorDelete:
    async def test_delete(self, tmp_path: Any) -> None:
        connector = LocalConnector(root=tmp_path)
        await connector.write("f.txt", b"data", "text/plain")
        await connector.delete("f.txt")
        assert not (tmp_path / "f.txt").exists()

    async def test_delete_missing_raises(self, tmp_path: Any) -> None:
        connector = LocalConnector(root=tmp_path)
        with pytest.raises(ConnectorNotFound):
            await connector.delete("missing.txt")


class TestLocalConnectorCheck:
    async def test_check_ok(self, tmp_path: Any) -> None:
        connector = LocalConnector(root=tmp_path)
        result = await connector.check()
        assert result.ok
        assert result.messages

    async def test_check_missing_root(self, tmp_path: Any) -> None:
        connector = LocalConnector(root=tmp_path / "does-not-exist")
        result = await connector.check()
        assert not result.ok


class TestLocalConnectorSignedUrl:
    connector_id = "11111111-1111-1111-1111-111111111111"

    async def test_signed_url_format(self, tmp_path: Any) -> None:
        (tmp_path / "f.txt").write_bytes(b"x")
        connector = LocalConnector(root=tmp_path, connector_id=self.connector_id)
        url = await connector.signed_url("f.txt", expires_in=60)
        assert url.startswith(f"/api/v1/storage/local/{self.connector_id}/f.txt?expires=")
        assert "&sig=" in url

    async def test_signed_url_signature_verifies(self, tmp_path: Any) -> None:
        (tmp_path / "f.txt").write_bytes(b"x")
        connector = LocalConnector(root=tmp_path, connector_id=self.connector_id)
        url = await connector.signed_url("f.txt", expires_in=60)
        query = parse_qs(urlparse(url).query)
        assert verify_storage_path(
            self.connector_id, "f.txt", int(query["expires"][0]), query["sig"][0]
        )

    async def test_signature_is_bound_to_the_path(self, tmp_path: Any) -> None:
        """Swapping the path in a valid URL must not keep the signature valid."""
        (tmp_path / "f.txt").write_bytes(b"x")
        connector = LocalConnector(root=tmp_path, connector_id=self.connector_id)
        url = await connector.signed_url("f.txt", expires_in=60)
        query = parse_qs(urlparse(url).query)
        assert not verify_storage_path(
            self.connector_id, "secrets.txt", int(query["expires"][0]), query["sig"][0]
        )

    async def test_signed_url_quotes_path(self, tmp_path: Any) -> None:
        (tmp_path / "a b.txt").write_bytes(b"x")
        connector = LocalConnector(root=tmp_path, connector_id=self.connector_id)
        url = await connector.signed_url("a b.txt")
        assert "a%20b.txt" in url

    async def test_signed_url_rejects_traversal(self, tmp_path: Any) -> None:
        connector = LocalConnector(root=tmp_path, connector_id=self.connector_id)
        with pytest.raises(ConnectorError):
            await connector.signed_url("../escape.txt")

    async def test_signed_url_without_a_connector_id_cannot_sign(self, tmp_path: Any) -> None:
        """A hand-built connector has no row id, so it has nothing to address."""
        (tmp_path / "f.txt").write_bytes(b"x")
        connector = LocalConnector(root=tmp_path)
        with pytest.raises(ConnectorError):
            await connector.signed_url("f.txt")

    async def test_write_url_carries_a_write_scoped_signature(self, tmp_path: Any) -> None:
        """A write URL is not a read URL with a flag: the signatures differ."""
        connector = LocalConnector(root=tmp_path, connector_id=self.connector_id)
        read_url = await connector.signed_url("new.jpg", expires_in=60)
        write_url = await connector.signed_url("new.jpg", expires_in=60, write=True)
        read_sig = parse_qs(urlparse(read_url).query)["sig"][0]
        write_sig = parse_qs(urlparse(write_url).query)["sig"][0]
        assert read_sig != write_sig
        assert urlparse(write_url).path == urlparse(read_url).path

    async def test_write_url_still_refuses_traversal(self, tmp_path: Any) -> None:
        connector = LocalConnector(root=tmp_path, connector_id=self.connector_id)
        with pytest.raises(ConnectorError):
            await connector.signed_url("../escape.jpg", write=True)

    async def test_public_base_url_makes_the_url_absolute(self, tmp_path: Any) -> None:
        (tmp_path / "f.txt").write_bytes(b"x")
        connector = LocalConnector(
            root=tmp_path,
            connector_id=self.connector_id,
            public_base_url="http://backend:8000/",
        )
        url = await connector.signed_url("f.txt")
        assert url.startswith(f"http://backend:8000/api/v1/storage/local/{self.connector_id}/f.txt")

    async def test_internal_base_url_only_for_internal_urls(self, tmp_path: Any) -> None:
        # A model service gets an absolute URL it can reach; the browser keeps
        # the root-relative one (APP_INTERNAL_API_URL).
        (tmp_path / "f.txt").write_bytes(b"x")
        connector = LocalConnector(
            root=tmp_path,
            connector_id=self.connector_id,
            internal_base_url="http://backend:8000/",
        )
        internal = await connector.signed_url("f.txt", internal=True)
        browser = await connector.signed_url("f.txt")
        assert internal.startswith(f"http://backend:8000/api/v1/storage/local/{self.connector_id}/")
        assert browser.startswith(f"/api/v1/storage/local/{self.connector_id}/")

    async def test_internal_api_url_setting_reaches_the_connector(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import get_settings
        from app.models import Connector, ConnectorIdentity, ConnectorType
        from app.services.storage import storage_for

        monkeypatch.setenv("APP_INTERNAL_API_URL", "http://backend:8000")
        get_settings.cache_clear()
        try:
            (tmp_path / "f.txt").write_bytes(b"x")
            row = Connector(
                id=uuid.UUID(self.connector_id),
                name="local",
                type=ConnectorType.LOCAL,
                identity_type=ConnectorIdentity.NONE,
                config={"root": str(tmp_path)},
            )
            async with storage_for(row) as storage:
                url = await storage.signed_url("f.txt", internal=True)
            assert url.startswith("http://backend:8000/api/v1/storage/local/")
        finally:
            get_settings.cache_clear()


class TestLocalConnectorPathTraversal:
    """SEC-4: local disk must never serve or accept a path outside its root."""

    async def test_rejects_dotdot_on_read(self, tmp_path: Any) -> None:
        connector = LocalConnector(root=tmp_path)
        with pytest.raises(ConnectorError):
            await connector.read("../../etc/passwd")

    async def test_rejects_dotdot_on_write(self, tmp_path: Any) -> None:
        connector = LocalConnector(root=tmp_path)
        with pytest.raises(ConnectorError):
            await connector.write("../escape.txt", b"x", "text/plain")

    async def test_rejects_dotdot_on_delete(self, tmp_path: Any) -> None:
        connector = LocalConnector(root=tmp_path)
        with pytest.raises(ConnectorError):
            await connector.delete("../escape.txt")

    async def test_rejects_absolute_path(self, tmp_path: Any) -> None:
        connector = LocalConnector(root=tmp_path)
        with pytest.raises(ConnectorError):
            await connector.read("/etc/passwd")

    async def test_rejects_absolute_path_on_write(self, tmp_path: Any) -> None:
        connector = LocalConnector(root=tmp_path)
        with pytest.raises(ConnectorError):
            await connector.write("/etc/passwd", b"x", "text/plain")

    async def test_rejects_empty_path(self, tmp_path: Any) -> None:
        connector = LocalConnector(root=tmp_path)
        with pytest.raises(ConnectorError):
            await connector.read("")

    async def test_rejects_symlink_escape(self, tmp_path_factory: Any) -> None:
        root = tmp_path_factory.mktemp("root")
        outside = tmp_path_factory.mktemp("outside")
        (outside / "secret.txt").write_bytes(b"secret")
        (root / "escape").symlink_to(outside)

        connector = LocalConnector(root=root)
        with pytest.raises(ConnectorError):
            await connector.read("escape/secret.txt")
        with pytest.raises(ConnectorError):
            await connector.write("escape/new.txt", b"x", "text/plain")

    async def test_rejects_symlinked_file_escape(self, tmp_path_factory: Any) -> None:
        root = tmp_path_factory.mktemp("root")
        outside = tmp_path_factory.mktemp("outside")
        secret = outside / "secret.txt"
        secret.write_bytes(b"secret")
        (root / "link.txt").symlink_to(secret)

        connector = LocalConnector(root=root)
        with pytest.raises(ConnectorError):
            await connector.read("link.txt")


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


class TestRegistry:
    @pytest.mark.parametrize(
        ("connector_type", "cls"),
        [
            ("local", LocalConnector),
            ("azure_blob", AzureBlobConnector),
            ("s3", S3Connector),
            ("gcs", GCSConnector),
            ("http", HTTPConnector),
        ],
    )
    async def test_get_connector_class_resolves_each_type(
        self, connector_type: str, cls: type
    ) -> None:
        assert get_connector_class(connector_type) is cls

    async def test_get_connector_class_unknown_raises(self) -> None:
        with pytest.raises(ConnectorConfigError):
            get_connector_class("ftp")

    async def test_build_connector_local(self, tmp_path: Any) -> None:
        connector = build_connector("local", {"root": str(tmp_path)})
        assert isinstance(connector, LocalConnector)

    async def test_build_connector_local_missing_root_raises(self) -> None:
        with pytest.raises(ConnectorConfigError):
            build_connector("local", {})

    async def test_build_connector_azure_blob(self) -> None:
        connector = build_connector(
            "azure_blob",
            {
                "account_url": "https://acct.blob.core.windows.net",
                "container": "data",
                "identity_type": "account_key",
            },
            secret="key123",
        )
        assert isinstance(connector, AzureBlobConnector)

    async def test_build_connector_azure_blob_missing_keys_raises(self) -> None:
        with pytest.raises(ConnectorConfigError):
            build_connector("azure_blob", {"container": "data"})

    async def test_build_connector_unknown_type_raises(self) -> None:
        with pytest.raises(ConnectorConfigError):
            build_connector("ftp", {})

    async def test_build_connector_http(self) -> None:
        http = build_connector("http", {"base_url": "https://cdn.example.com/d"})
        assert isinstance(http, HTTPConnector)
        with pytest.raises(ConnectorConfigError, match="base_url"):
            build_connector("http", {})

    async def test_build_connector_s3_and_gcs(self) -> None:
        s3 = build_connector("s3", {"bucket": "b", "identity_type": "none"})
        assert isinstance(s3, S3Connector)
        gcs = build_connector("gcs", {"bucket": "b", "identity_type": "none"})
        assert isinstance(gcs, GCSConnector)
        for connector_type in ("s3", "gcs"):
            with pytest.raises(ConnectorConfigError, match="bucket"):
                build_connector(connector_type, {})


# --------------------------------------------------------------------------
# AzureBlobConnector — pure logic only, Azure SDK client mocked
# --------------------------------------------------------------------------


def _make_azure_connector(**overrides: Any) -> AzureBlobConnector:
    defaults: dict[str, Any] = {
        "account_url": "https://acct.blob.core.windows.net",
        "container": "data",
        "identity_type": "managed_identity",
    }
    defaults.update(overrides)
    return AzureBlobConnector(**defaults)


class TestAzureBlobCredentialFactory:
    async def test_managed_identity_default_credential(self) -> None:
        connector = _make_azure_connector(identity_type="managed_identity")
        assert isinstance(connector._build_credential(), DefaultAzureCredential)

    async def test_managed_identity_with_client_id(self) -> None:
        connector = _make_azure_connector(identity_type="managed_identity", client_id="cid")
        assert isinstance(connector._build_credential(), ManagedIdentityCredential)

    async def test_service_principal_ok(self) -> None:
        connector = _make_azure_connector(
            identity_type="service_principal",
            tenant_id="tid",
            client_id="cid",
            secret="csecret",
        )
        assert isinstance(connector._build_credential(), ClientSecretCredential)

    async def test_service_principal_missing_fields_raises(self) -> None:
        connector = _make_azure_connector(identity_type="service_principal")
        with pytest.raises(ConnectorConfigError):
            connector._build_credential()

    async def test_account_key_ok(self) -> None:
        connector = _make_azure_connector(identity_type="account_key", secret="k")
        credential = connector._build_credential()
        # The dict form, so the SDK is told the account name rather than
        # inferring it from the URL (which fails for path-style endpoints).
        assert credential == {"account_name": connector._account_name, "account_key": "k"}

    async def test_account_key_missing_secret_raises(self) -> None:
        connector = _make_azure_connector(identity_type="account_key")
        with pytest.raises(ConnectorConfigError):
            connector._build_credential()

    async def test_sas_token_ok(self) -> None:
        connector = _make_azure_connector(identity_type="sas_token", secret="sv=abc")
        assert isinstance(connector._build_credential(), AzureSasCredential)

    async def test_sas_token_missing_secret_raises(self) -> None:
        connector = _make_azure_connector(identity_type="sas_token")
        with pytest.raises(ConnectorConfigError):
            connector._build_credential()

    async def test_unsupported_identity_type_raises(self) -> None:
        connector = _make_azure_connector(identity_type="iam_role")
        with pytest.raises(ConnectorConfigError):
            connector._build_credential()

    async def test_account_name_parsed_from_url(self) -> None:
        connector = _make_azure_connector(account_url="https://myacct.blob.core.windows.net/")
        assert connector._account_name == "myacct"


class _FakeBlobItem:
    def __init__(
        self,
        name: str,
        size: int = 10,
        etag: str = '"abc"',
        content_type: str | None = None,
    ) -> None:
        self.name = name
        self.size = size
        self.etag = etag
        self.last_modified = None
        self.content_settings = SimpleNamespace(content_type=content_type) if content_type else None


class TestAzureBlobList:
    async def test_list_applies_prefix_and_glob_client_side(self) -> None:
        connector = _make_azure_connector(identity_type="account_key", secret="k")

        items = [
            _FakeBlobItem("images/a.jpg"),
            _FakeBlobItem("images/b.png"),
            _FakeBlobItem("docs/readme.txt"),
        ]

        async def fake_list_blobs(name_starts_with: str | None = None) -> Any:
            for item in items:
                if name_starts_with and not item.name.startswith(name_starts_with):
                    continue
                yield item

        container_client = MagicMock()
        container_client.list_blobs = fake_list_blobs

        client = AsyncMock()
        client.get_container_client = MagicMock(return_value=container_client)
        connector._get_client = AsyncMock(return_value=client)  # type: ignore[method-assign]

        results = [obj async for obj in connector.list("images", glob="*.jpg")]
        assert [r.path for r in results] == ["images/a.jpg"]

    async def test_list_etag_is_unquoted(self) -> None:
        connector = _make_azure_connector(identity_type="account_key", secret="k")

        async def fake_list_blobs(name_starts_with: str | None = None) -> Any:
            yield _FakeBlobItem("a.txt", etag='"quoted-etag"')

        container_client = MagicMock()
        container_client.list_blobs = fake_list_blobs
        client = AsyncMock()
        client.get_container_client = MagicMock(return_value=container_client)
        connector._get_client = AsyncMock(return_value=client)  # type: ignore[method-assign]

        [info] = [obj async for obj in connector.list("")]
        assert info.etag == "quoted-etag"


class TestAzureBlobReadDelete:
    async def test_read_maps_not_found(self) -> None:
        connector = _make_azure_connector(identity_type="account_key", secret="k")
        blob_client = AsyncMock()
        blob_client.download_blob = AsyncMock(side_effect=ResourceNotFoundError("nope"))
        client = AsyncMock()
        client.get_blob_client = MagicMock(return_value=blob_client)
        connector._get_client = AsyncMock(return_value=client)  # type: ignore[method-assign]

        with pytest.raises(ConnectorNotFound):
            await connector.read("missing.txt")

    async def test_delete_maps_not_found(self) -> None:
        connector = _make_azure_connector(identity_type="account_key", secret="k")
        blob_client = AsyncMock()
        blob_client.delete_blob = AsyncMock(side_effect=ResourceNotFoundError("nope"))
        client = AsyncMock()
        client.get_blob_client = MagicMock(return_value=blob_client)
        connector._get_client = AsyncMock(return_value=client)  # type: ignore[method-assign]

        with pytest.raises(ConnectorNotFound):
            await connector.delete("missing.txt")


class TestAzureBlobCheck:
    async def test_check_ok_with_cors_allowed(self) -> None:
        connector = _make_azure_connector(
            identity_type="account_key",
            secret="k",
            frontend_origin="https://app.example.com",
        )
        container_client = AsyncMock()
        container_client.get_container_properties = AsyncMock(return_value={})
        client = AsyncMock()
        client.get_container_client = MagicMock(return_value=container_client)
        client.get_service_properties = AsyncMock(
            return_value={"cors": [SimpleNamespace(allowed_origins=["https://app.example.com"])]}
        )
        connector._get_client = AsyncMock(return_value=client)  # type: ignore[method-assign]

        result = await connector.check()
        assert result.ok
        assert any("CORS allows" in m and "reads only" in m for m in result.messages)

    async def test_check_cors_reports_put_for_browser_uploads(self) -> None:
        connector = _make_azure_connector(
            identity_type="account_key",
            secret="k",
            frontend_origin="https://app.example.com",
        )
        container_client = AsyncMock()
        container_client.get_container_properties = AsyncMock(return_value={})
        client = AsyncMock()
        client.get_container_client = MagicMock(return_value=container_client)
        client.get_service_properties = AsyncMock(
            return_value={
                "cors": [
                    SimpleNamespace(
                        allowed_origins=["*"], allowed_methods=["GET", "put", "OPTIONS"]
                    )
                ]
            }
        )
        connector._get_client = AsyncMock(return_value=client)  # type: ignore[method-assign]

        result = await connector.check()
        assert result.ok
        assert any("GET and PUT" in m for m in result.messages)

    async def test_check_cors_warning_when_origin_not_allowed(self) -> None:
        connector = _make_azure_connector(
            identity_type="account_key",
            secret="k",
            frontend_origin="https://app.example.com",
        )
        container_client = AsyncMock()
        container_client.get_container_properties = AsyncMock(return_value={})
        client = AsyncMock()
        client.get_container_client = MagicMock(return_value=container_client)
        client.get_service_properties = AsyncMock(
            return_value={"cors": [SimpleNamespace(allowed_origins=["https://other.example.com"])]}
        )
        connector._get_client = AsyncMock(return_value=client)  # type: ignore[method-assign]

        result = await connector.check()
        assert result.ok  # container itself is reachable
        assert any("CORS warning" in m for m in result.messages)

    async def test_check_container_not_found(self) -> None:
        connector = _make_azure_connector(identity_type="account_key", secret="k")
        container_client = AsyncMock()
        container_client.get_container_properties = AsyncMock(
            side_effect=ResourceNotFoundError("nope")
        )
        client = AsyncMock()
        client.get_container_client = MagicMock(return_value=container_client)
        connector._get_client = AsyncMock(return_value=client)  # type: ignore[method-assign]

        result = await connector.check()
        assert not result.ok

    async def test_check_cors_unreadable_reports_message_not_failure(self) -> None:
        connector = _make_azure_connector(
            identity_type="account_key",
            secret="k",
            frontend_origin="https://app.example.com",
        )
        container_client = AsyncMock()
        container_client.get_container_properties = AsyncMock(return_value={})
        client = AsyncMock()
        client.get_container_client = MagicMock(return_value=container_client)
        client.get_service_properties = AsyncMock(side_effect=ClientAuthenticationError("nope"))
        connector._get_client = AsyncMock(return_value=client)  # type: ignore[method-assign]

        result = await connector.check()
        assert result.ok
        assert any("could not read CORS" in m for m in result.messages)


class TestAzureBlobSignedUrl:
    async def test_sas_token_identity_reuses_secret_as_is(self) -> None:
        connector = _make_azure_connector(identity_type="sas_token", secret="sv=2020&sig=abc")
        url = await connector.signed_url("images/a.jpg")
        assert url == "https://acct.blob.core.windows.net/data/images/a.jpg?sv=2020&sig=abc"

    async def test_account_key_generates_sas_via_generate_blob_sas(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connector = _make_azure_connector(identity_type="account_key", secret="base64key==")
        monkeypatch.setattr(
            "app.connectors.azure_blob.generate_blob_sas", lambda **_kwargs: "generated-sas"
        )
        url = await connector.signed_url("a.jpg", expires_in=120, write=True)
        assert url == "https://acct.blob.core.windows.net/data/a.jpg?generated-sas"

    async def test_managed_identity_uses_and_caches_user_delegation_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connector = _make_azure_connector(identity_type="managed_identity")
        client = AsyncMock()
        client.get_user_delegation_key = AsyncMock(return_value="fake-delegation-key")
        connector._get_client = AsyncMock(return_value=client)  # type: ignore[method-assign]
        monkeypatch.setattr(
            "app.connectors.azure_blob.generate_blob_sas", lambda **_kwargs: "sas-1"
        )

        url1 = await connector.signed_url("a.jpg")
        assert url1.endswith("?sas-1")
        assert client.get_user_delegation_key.await_count == 1

        url2 = await connector.signed_url("b.jpg")
        assert url2.endswith("?sas-1")
        # The cached delegation key is reused — no second fetch.
        assert client.get_user_delegation_key.await_count == 1

    async def test_missing_secret_raises_for_account_key(self) -> None:
        connector = _make_azure_connector(identity_type="account_key")
        with pytest.raises(ConnectorConfigError):
            await connector.signed_url("a.jpg")

    async def test_missing_secret_raises_for_sas_token(self) -> None:
        connector = _make_azure_connector(identity_type="sas_token")
        with pytest.raises(ConnectorConfigError):
            await connector.signed_url("a.jpg")
