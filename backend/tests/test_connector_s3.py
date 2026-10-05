"""Tests for `app.connectors.s3` (`S3Connector`, `build_s3`).

Runs against a real `moto` `ThreadedMotoServer` (a genuine HTTP server, random
free port) rather than mocking the SDK, so `aioboto3` talks real HTTP end to
end — the same shape of coverage `azure_blob` gets from Azurite in the
compose stack, but self-contained for a fast unit-test run.
"""

from __future__ import annotations

import json
import socket
import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from moto.server import ThreadedMotoServer

from app.connectors.errors import ConnectorConfigError, ConnectorNotFound
from app.connectors.s3 import S3Connector, build_s3

DUMMY_ACCESS_KEY = "testing"
DUMMY_SECRET_KEY = "testing"
ACCESS_KEY_SECRET = json.dumps(
    {"access_key_id": DUMMY_ACCESS_KEY, "secret_access_key": DUMMY_SECRET_KEY}
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="module")
def moto_server() -> Iterator[str]:
    port = _free_port()
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port)
    server.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.stop()


@pytest.fixture
async def bucket(moto_server: str) -> AsyncIterator[str]:
    """A freshly created bucket, unique per test so tests never share state."""
    import aioboto3

    name = f"test-bucket-{uuid.uuid4().hex[:8]}"
    session = aioboto3.Session()
    async with session.client(
        "s3",
        endpoint_url=moto_server,
        region_name="us-east-1",
        aws_access_key_id=DUMMY_ACCESS_KEY,
        aws_secret_access_key=DUMMY_SECRET_KEY,
    ) as client:
        await client.create_bucket(Bucket=name)
    yield name


def _connector(moto_server: str, bucket: str, **kwargs: object) -> S3Connector:
    return S3Connector(
        bucket=bucket,
        identity_type="access_key",
        secret=ACCESS_KEY_SECRET,
        region="us-east-1",
        endpoint_url=moto_server,
        force_path_style=True,
        **kwargs,  # type: ignore[arg-type]
    )


class TestWriteReadDelete:
    async def test_write_then_read_full(self, moto_server: str, bucket: str) -> None:
        connector = _connector(moto_server, bucket)
        await connector.write("a.txt", b"hello world", "text/plain")
        assert await connector.read("a.txt") == b"hello world"
        await connector.aclose()

    async def test_read_range(self, moto_server: str, bucket: str) -> None:
        connector = _connector(moto_server, bucket)
        await connector.write("f.bin", b"0123456789", "application/octet-stream")
        assert await connector.read("f.bin", start=2, end=5) == b"234"
        await connector.aclose()

    async def test_read_start_only(self, moto_server: str, bucket: str) -> None:
        connector = _connector(moto_server, bucket)
        await connector.write("f.bin", b"0123456789", "application/octet-stream")
        assert await connector.read("f.bin", start=7) == b"789"
        await connector.aclose()

    async def test_read_missing_raises_not_found(self, moto_server: str, bucket: str) -> None:
        connector = _connector(moto_server, bucket)
        with pytest.raises(ConnectorNotFound):
            await connector.read("missing.bin")
        await connector.aclose()

    async def test_delete(self, moto_server: str, bucket: str) -> None:
        connector = _connector(moto_server, bucket)
        await connector.write("f.txt", b"data", "text/plain")
        await connector.delete("f.txt")
        with pytest.raises(ConnectorNotFound):
            await connector.read("f.txt")
        await connector.aclose()

    async def test_delete_missing_raises_not_found(self, moto_server: str, bucket: str) -> None:
        connector = _connector(moto_server, bucket)
        with pytest.raises(ConnectorNotFound):
            await connector.delete("missing.txt")
        await connector.aclose()


class TestList:
    async def test_list_all(self, moto_server: str, bucket: str) -> None:
        connector = _connector(moto_server, bucket)
        await connector.write("a.txt", b"a", "text/plain")
        await connector.write("images/b.jpg", b"b", "image/jpeg")
        await connector.write("images/nested/c.png", b"c", "image/png")

        results = [obj async for obj in connector.list("")]
        paths = sorted(o.path for o in results)
        assert paths == ["a.txt", "images/b.jpg", "images/nested/c.png"]
        await connector.aclose()

    async def test_list_with_prefix(self, moto_server: str, bucket: str) -> None:
        connector = _connector(moto_server, bucket)
        await connector.write("a.txt", b"a", "text/plain")
        await connector.write("images/b.jpg", b"b", "image/jpeg")

        results = [obj async for obj in connector.list("images")]
        assert [o.path for o in results] == ["images/b.jpg"]
        await connector.aclose()

    async def test_list_with_glob(self, moto_server: str, bucket: str) -> None:
        connector = _connector(moto_server, bucket)
        await connector.write("images/a.jpg", b"a", "image/jpeg")
        await connector.write("images/b.png", b"b", "image/png")

        results = [obj async for obj in connector.list("", glob="*.jpg")]
        assert [o.path for o in results] == ["images/a.jpg"]
        await connector.aclose()

    async def test_list_pages_with_small_page_size(self, moto_server: str, bucket: str) -> None:
        connector = _connector(moto_server, bucket, page_size=2)
        for i in range(5):
            await connector.write(f"f{i}.txt", b"x", "text/plain")

        results = [obj async for obj in connector.list("")]
        assert sorted(o.path for o in results) == [f"f{i}.txt" for i in range(5)]
        await connector.aclose()

    async def test_list_etag_and_size(self, moto_server: str, bucket: str) -> None:
        connector = _connector(moto_server, bucket)
        await connector.write("x.txt", b"hello", "text/plain")
        [info] = [obj async for obj in connector.list("")]
        assert info.size_bytes == 5
        assert info.etag is not None
        assert '"' not in info.etag
        assert info.last_modified is not None
        await connector.aclose()


class TestSignedUrl:
    async def test_signed_get_is_fetchable(self, moto_server: str, bucket: str) -> None:
        connector = _connector(moto_server, bucket)
        await connector.write("cat.jpg", b"meow", "image/jpeg")
        url = await connector.signed_url("cat.jpg", expires_in=60)

        async with httpx.AsyncClient() as http_client:
            response = await http_client.get(url)
        assert response.status_code == 200
        assert response.content == b"meow"
        await connector.aclose()

    async def test_signed_put_works(self, moto_server: str, bucket: str) -> None:
        connector = _connector(moto_server, bucket)
        url = await connector.signed_url("uploaded.txt", expires_in=60, write=True)

        async with httpx.AsyncClient() as http_client:
            response = await http_client.put(url, content=b"uploaded via signed url")
        assert response.status_code == 200
        assert await connector.read("uploaded.txt") == b"uploaded via signed url"
        await connector.aclose()

    async def test_public_endpoint_used_by_default(self, moto_server: str, bucket: str) -> None:
        internal = "http://internal.invalid:1234"
        connector = S3Connector(
            bucket=bucket,
            identity_type="access_key",
            secret=ACCESS_KEY_SECRET,
            region="us-east-1",
            endpoint_url=internal,
            public_endpoint_url=moto_server,
            force_path_style=True,
        )
        url = await connector.signed_url("cat.jpg", expires_in=60)
        assert url.startswith(moto_server)
        assert "internal.invalid" not in url
        await connector.aclose()

    async def test_internal_flag_uses_internal_endpoint(
        self, moto_server: str, bucket: str
    ) -> None:
        public = "http://public.invalid:5678"
        connector = S3Connector(
            bucket=bucket,
            identity_type="access_key",
            secret=ACCESS_KEY_SECRET,
            region="us-east-1",
            endpoint_url=moto_server,
            public_endpoint_url=public,
            force_path_style=True,
        )
        url = await connector.signed_url("cat.jpg", expires_in=60, internal=True)
        assert url.startswith(moto_server)
        assert "public.invalid" not in url
        await connector.aclose()

    async def test_none_identity_returns_plain_url(self, moto_server: str, bucket: str) -> None:
        connector = S3Connector(
            bucket=bucket,
            identity_type="none",
            region="us-east-1",
            endpoint_url=moto_server,
            public_endpoint_url="http://public.invalid:5678",
            force_path_style=True,
        )
        url = await connector.signed_url("cat.jpg")
        assert url == f"http://public.invalid:5678/{bucket}/cat.jpg"
        assert "X-Amz-Signature" not in url
        internal_url = await connector.signed_url("cat.jpg", internal=True)
        assert internal_url == f"{moto_server}/{bucket}/cat.jpg"
        await connector.aclose()


class TestAccessKeyIdentityErrors:
    async def test_missing_secret_raises_config_error(self) -> None:
        connector = S3Connector(bucket="b", identity_type="access_key", secret=None)
        with pytest.raises(ConnectorConfigError):
            await connector.read("x.txt")
        await connector.aclose()

    async def test_malformed_json_raises_config_error(self) -> None:
        connector = S3Connector(bucket="b", identity_type="access_key", secret="not json")
        with pytest.raises(ConnectorConfigError):
            await connector.read("x.txt")
        await connector.aclose()

    async def test_missing_keys_raises_config_error(self) -> None:
        connector = S3Connector(
            bucket="b", identity_type="access_key", secret=json.dumps({"access_key_id": "a"})
        )
        with pytest.raises(ConnectorConfigError):
            await connector.read("x.txt")
        await connector.aclose()

    async def test_unsupported_identity_type_raises_config_error(self) -> None:
        connector = S3Connector(bucket="b", identity_type="bogus")
        with pytest.raises(ConnectorConfigError):
            await connector.read("x.txt")
        await connector.aclose()


class TestCheck:
    async def test_check_ok_without_frontend_origin(self, moto_server: str, bucket: str) -> None:
        connector = _connector(moto_server, bucket)
        result = await connector.check()
        assert result.ok
        assert result.messages
        await connector.aclose()

    async def test_check_missing_bucket(self, moto_server: str) -> None:
        connector = S3Connector(
            bucket="does-not-exist",
            identity_type="access_key",
            secret=ACCESS_KEY_SECRET,
            region="us-east-1",
            endpoint_url=moto_server,
            force_path_style=True,
        )
        result = await connector.check()
        assert not result.ok
        await connector.aclose()

    async def test_check_warns_when_cors_missing(self, moto_server: str, bucket: str) -> None:
        connector = _connector(moto_server, bucket, frontend_origin="https://app.example.com")
        result = await connector.check()
        assert result.ok
        assert any("CORS" in message for message in result.messages)
        await connector.aclose()

    async def test_check_reports_cors_allowed(self, moto_server: str, bucket: str) -> None:
        import aioboto3

        session = aioboto3.Session()
        async with session.client(
            "s3",
            endpoint_url=moto_server,
            region_name="us-east-1",
            aws_access_key_id=DUMMY_ACCESS_KEY,
            aws_secret_access_key=DUMMY_SECRET_KEY,
        ) as client:
            await client.put_bucket_cors(
                Bucket=bucket,
                CORSConfiguration={
                    "CORSRules": [
                        {
                            "AllowedOrigins": ["https://app.example.com"],
                            "AllowedMethods": ["GET", "PUT"],
                        }
                    ]
                },
            )

        connector = _connector(moto_server, bucket, frontend_origin="https://app.example.com")
        result = await connector.check()
        assert result.ok
        assert any(
            "CORS allows" in message and "GET and PUT" in message for message in result.messages
        )
        await connector.aclose()


class TestBuildS3:
    def test_requires_bucket(self) -> None:
        with pytest.raises(ConnectorConfigError):
            build_s3({})

    def test_builds_with_defaults(self) -> None:
        connector = build_s3({"bucket": "media"}, secret=None)
        assert isinstance(connector, S3Connector)
        assert connector._bucket == "media"
        assert connector._identity_type == "iam_role"

    def test_builds_with_all_keys(self) -> None:
        connector = build_s3(
            {
                "bucket": "media",
                "identity_type": "access_key",
                "region": "eu-west-1",
                "endpoint_url": "http://minio:9000",
                "public_endpoint_url": "http://localhost:9000",
                "force_path_style": True,
                "frontend_origin": "https://app.example.com",
            },
            secret=ACCESS_KEY_SECRET,
        )
        assert connector._endpoint_url == "http://minio:9000"
        assert connector._public_endpoint_url == "http://localhost:9000"
        assert connector._force_path_style is True
