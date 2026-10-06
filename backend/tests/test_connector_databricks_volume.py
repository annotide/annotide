"""`app.connectors.databricks_volume` (§3) against a scripted Files API, and its proxy."""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool

from app.connectors.databricks_volume import DatabricksVolumeConnector
from app.connectors.errors import ConnectorAuthError, ConnectorConfigError, ConnectorNotFound
from app.connectors.registry import build_connector
from app.db.base import Base
from app.db.session import get_session
from app.main import create_app
from app.models import Connector, ConnectorIdentity, ConnectorType, Organization

HOST = "https://adb-1.azuredatabricks.example"
VOLUME = "/Volumes/main/vision/raw"
FILES = "/api/2.0/fs/files/Volumes/main/vision/raw"
DIRS = "/api/2.0/fs/directories/Volumes/main/vision/raw"


@compiles(CITEXT, "sqlite")  # pragma: no cover - exercised implicitly
def _compile_citext_as_varchar(element: object, compiler: object, **kw: object) -> str:
    return "VARCHAR"


class FakeFiles:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], httpx.Response] = {}
        self.requests: list[httpx.Request] = []
        self.files: dict[str, bytes] = {}
        self.honour_range = True

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/oidc/v1/token":
            return httpx.Response(200, json={"access_token": "oauth-token", "expires_in": 3600})
        if request.method == "PUT" and path.startswith(FILES):
            self.files[path] = request.content
            return httpx.Response(204)
        if request.method == "HEAD" and path in self.files:
            return httpx.Response(200, headers={"Content-Length": str(len(self.files[path]))})
        if request.method == "GET" and path in self.files:
            body = self.files[path]
            wanted = request.headers.get("Range")
            if wanted and self.honour_range:
                first, _, last = wanted.removeprefix("bytes=").partition("-")
                part = body[int(first) : int(last) + 1 if last else None]
                return httpx.Response(206, content=part)
            return httpx.Response(200, content=body)
        token = request.url.params.get("page_token")
        reply = self.routes.get((request.method, f"{path}?{token}" if token else path))
        return reply if reply is not None else httpx.Response(404, json={"error_code": "NOT_FOUND"})

    def connector(self, **kwargs: Any) -> DatabricksVolumeConnector:
        options: dict[str, Any] = {
            "host": HOST,
            "volume_path": VOLUME,
            "identity_type": "access_key",
            "secret": "dapi-token",
            "connector_id": "c-1",
        }
        options.update(kwargs)
        return DatabricksVolumeConnector(transport=httpx.MockTransport(self.handle), **options)


def _entry(path: str, *, directory: bool = False, size: int = 4) -> dict[str, Any]:
    return {
        "path": f"{VOLUME}/{path}",
        "is_directory": directory,
        "file_size": size,
        "last_modified": 1790000000000,
        "name": path.rsplit("/", 1)[-1],
    }


async def test_list_pages_and_walks_directories() -> None:
    files = FakeFiles()
    files.routes[("GET", f"{DIRS}/imgs/")] = httpx.Response(
        200,
        json={
            "contents": [_entry("imgs/a.jpg"), _entry("imgs/sub", directory=True)],
            "next_page_token": "p2",
        },
    )
    files.routes[("GET", f"{DIRS}/imgs/?p2")] = httpx.Response(
        200, json={"contents": [_entry("imgs/c.jpg", size=2)]}
    )
    files.routes[("GET", f"{DIRS}/imgs/sub/")] = httpx.Response(
        200, json={"contents": [_entry("imgs/sub/b.jpg", size=9), _entry("imgs/sub/n.txt")]}
    )
    connector = files.connector()

    found = [(o.path, o.size_bytes) async for o in connector.list("imgs/", glob="*.jpg")]
    await connector.aclose()

    assert found == [("imgs/a.jpg", 4), ("imgs/c.jpg", 2), ("imgs/sub/b.jpg", 9)]
    assert all(r.headers["Authorization"] == "Bearer dapi-token" for r in files.requests)


async def test_a_missing_directory_lists_nothing() -> None:
    connector = FakeFiles().connector()
    assert [o async for o in connector.list("nope/")] == []
    await connector.aclose()


async def test_read_write_delete_round_trip_and_range() -> None:
    files = FakeFiles()
    files.routes[("DELETE", f"{FILES}/out/r.json")] = httpx.Response(204)
    files.routes[("GET", f"{FILES}/missing.jpg")] = httpx.Response(404)
    files.routes[("GET", f"{FILES}/locked.jpg")] = httpx.Response(403)
    connector = files.connector()

    await connector.write("out/r.json", b'{"a":1}', "application/json")
    put = files.requests[-1]
    assert put.url.params["overwrite"] == "true"
    assert await connector.read("out/r.json") == b'{"a":1}'
    assert await connector.read("out/r.json", start=2, end=5) == b'a":'
    assert files.requests[-1].headers["Range"] == "bytes=2-4"
    files.honour_range = False
    assert await connector.read("out/r.json", start=2, end=5) == b'a":'
    assert await connector.size("out/r.json") == 7
    assert files.requests[-1].method == "HEAD"
    await connector.delete("out/r.json")
    with pytest.raises(ConnectorNotFound):
        await connector.read("missing.jpg")
    with pytest.raises(ConnectorAuthError):
        await connector.read("locked.jpg")
    await connector.aclose()


async def test_service_principal_gets_and_caches_an_oauth_token() -> None:
    files = FakeFiles()
    files.routes[("GET", f"{DIRS}/")] = httpx.Response(200, json={"contents": []})
    connector = files.connector(identity_type="service_principal", client_id="sp-1", secret="s")
    assert (await connector.check()).ok
    assert (await connector.check()).ok
    tokens = [r for r in files.requests if r.url.path == "/oidc/v1/token"]
    assert len(tokens) == 1
    assert tokens[0].headers["Authorization"].startswith("Basic ")
    assert files.requests[-1].headers["Authorization"] == "Bearer oauth-token"
    await connector.aclose()


async def test_signed_url_points_at_the_platform_proxy() -> None:
    connector = FakeFiles().connector()
    url = await connector.signed_url("imgs/a b.jpg", expires_in=60)
    parts = urlsplit(url)
    assert parts.path == "/api/v1/storage/proxy/c-1/imgs/a%20b.jpg"
    query = parse_qs(parts.query)
    assert int(query["expires"][0]) <= int(time.time()) + 60
    assert query["sig"]
    write = await connector.signed_url("imgs/a.jpg", write=True)
    assert parse_qs(urlsplit(write).query)["sig"] != query["sig"]
    await connector.aclose()


@pytest.mark.parametrize(
    ("config", "secret", "message"),
    [
        ({"volume_path": VOLUME}, "t", "missing required key"),
        ({"host": "adb.example", "volume_path": VOLUME}, "t", "http"),
        ({"host": HOST, "volume_path": "/mnt/raw"}, "t", "/Volumes/"),
        ({"host": HOST, "volume_path": VOLUME}, None, "token or OAuth secret"),
        (
            {"host": HOST, "volume_path": VOLUME, "identity_type": "service_principal"},
            "s",
            "client_id",
        ),
        ({"host": HOST, "volume_path": VOLUME, "identity_type": "none"}, "s", "must be one of"),
    ],
)
def test_build_validates(config: dict[str, Any], secret: str | None, message: str) -> None:
    with pytest.raises(ConnectorConfigError, match=message):
        build_connector("databricks_volume", config, secret)


async def test_the_proxy_serves_a_volume_file_against_its_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with maker() as session:
        org = Organization(name="Acme", slug=f"acme-{uuid4().hex[:6]}")
        session.add(org)
        await session.flush()
        row = Connector(
            organization_id=org.id,
            name="volume",
            type=ConnectorType.DATABRICKS_VOLUME,
            identity_type=ConnectorIdentity.ACCESS_KEY,
            config={"host": HOST, "volume_path": VOLUME},
        )
        session.add(row)
        await session.commit()

    files = FakeFiles()
    files.files[f"{FILES}/imgs/a.jpg"] = b"jpeg"
    fake = files.connector(connector_id=str(row.id))

    @asynccontextmanager
    async def fake_storage_for(connector: Connector) -> AsyncIterator[DatabricksVolumeConnector]:
        yield fake

    monkeypatch.setattr("app.api.v1.storage.storage_for", fake_storage_for)
    app: FastAPI = create_app()

    async def _get_session() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = _get_session
    client = TestClient(app)

    url = await fake.signed_url("imgs/a.jpg")
    response = client.get(url)
    assert response.status_code == 200
    assert response.content == b"jpeg"
    assert response.headers["content-type"] == "image/jpeg"
    assert client.get(url.replace("sig=", "sig=x")).status_code == 403

    # Seeking in a long recording: only the asked-for bytes leave the volume.
    files.files[f"{FILES}/audio/long.wav"] = bytes(range(256)) * 4
    audio = await fake.signed_url("audio/long.wav")
    partial = client.get(audio, headers={"Range": "bytes=1000-"})
    assert partial.status_code == 206
    assert partial.content == (bytes(range(256)) * 4)[1000:]
    assert partial.headers["content-range"] == "bytes 1000-1023/1024"
    assert files.requests[-1].headers["Range"] == "bytes=1000-1023"

    upload = await fake.signed_url("imgs/new.png", write=True)
    assert client.put(upload, content=b"png").status_code == 204
    assert files.files[f"{FILES}/imgs/new.png"] == b"png"
    await engine.dispose()
