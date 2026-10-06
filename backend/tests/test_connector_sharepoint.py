"""`app.connectors.sharepoint` (§3) against a scripted Microsoft Graph."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.connectors.errors import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorError,
    ConnectorNotFound,
    UnsupportedOperation,
)
from app.connectors.registry import build_connector
from app.connectors.sharepoint import UPLOAD_CHUNK, SharePointConnector

GRAPH = "https://graph.example/v1.0"
DRIVE = "b!drive"


class FakeGraph:
    """Routes `(method, path)` to replies; records requests."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], list[httpx.Response]] = {}
        self.requests: list[httpx.Request] = []

    def on(self, method: str, path: str, *replies: httpx.Response) -> None:
        self.routes[(method, path)] = list(replies)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.raw_path.decode().split("?", 1)[0]
        replies = self.routes.get((request.method, path))
        if not replies:
            return httpx.Response(404, json={"error": {"code": "itemNotFound"}})
        return replies.pop(0) if len(replies) > 1 else replies[0]

    def connector(self, sleeps: list[float] | None = None) -> SharePointConnector:
        async def token() -> str:
            return "graph-token"

        async def sleep(seconds: float) -> None:
            if sleeps is not None:
                sleeps.append(seconds)

        return SharePointConnector(
            drive_id=DRIVE,
            identity_type="service_principal",
            graph_url=GRAPH,
            token_provider=token,
            transport=httpx.MockTransport(self.handle),
            sleep=sleep,
        )


def _children(*entries: dict[str, Any], next_link: str | None = None) -> httpx.Response:
    body: dict[str, Any] = {"value": list(entries)}
    if next_link:
        body["@odata.nextLink"] = next_link
    return httpx.Response(200, json=body)


def _file(name: str, size: int = 3) -> dict[str, Any]:
    return {
        "name": name,
        "size": size,
        "eTag": f'"{name}-1"',
        "lastModifiedDateTime": "2026-10-01T08:00:00Z",
        "file": {"mimeType": "image/jpeg"},
    }


ROOT = "/v1.0/drives/b%21drive/root"


async def test_list_walks_folders_pages_and_glob() -> None:
    graph = FakeGraph()
    graph.on(
        "GET",
        f"{ROOT}:/Shared%20Documents:/children",
        _children(_file("a.jpg"), {"name": "sub", "folder": {}}, next_link=f"{GRAPH}/page2"),
    )
    graph.on("GET", "/v1.0/page2", _children(_file("notes.txt")))
    graph.on("GET", f"{ROOT}:/Shared%20Documents/sub:/children", _children(_file("b.jpg", 9)))
    connector = graph.connector()

    found = [o async for o in connector.list("Shared Documents/", glob="*.jpg")]
    await connector.aclose()

    assert [(o.path, o.size_bytes) for o in found] == [
        ("Shared Documents/a.jpg", 3),
        ("Shared Documents/sub/b.jpg", 9),
    ]
    assert found[0].etag == '"a.jpg-1"'
    assert found[0].content_type == "image/jpeg"
    assert found[0].last_modified is not None
    assert all(r.headers["Authorization"] == "Bearer graph-token" for r in graph.requests)


async def test_a_missing_folder_lists_nothing() -> None:
    connector = FakeGraph().connector()
    assert [o async for o in connector.list("nope/")] == []
    await connector.aclose()


async def test_read_with_a_range_and_errors() -> None:
    graph = FakeGraph()
    graph.on("GET", f"{ROOT}:/a.jpg:/content", httpx.Response(206, content=b"bc"))
    graph.on("GET", f"{ROOT}:/secret.jpg:/content", httpx.Response(403))
    connector = graph.connector()

    assert await connector.read("a.jpg", start=1, end=3) == b"bc"
    assert graph.requests[-1].headers["Range"] == "bytes=1-2"
    with pytest.raises(ConnectorNotFound):
        await connector.read("gone.jpg")
    with pytest.raises(ConnectorAuthError):
        await connector.read("secret.jpg")
    await connector.aclose()


async def test_throttling_is_retried_after_retry_after() -> None:
    graph = FakeGraph()
    graph.on(
        "GET",
        f"{ROOT}:/a.jpg:/content",
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200, content=b"ok"),
    )
    sleeps: list[float] = []
    connector = graph.connector(sleeps)
    assert await connector.read("a.jpg") == b"ok"
    assert sleeps == [7.0]
    await connector.aclose()


async def test_small_and_large_writes_and_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = FakeGraph()
    graph.on("PUT", f"{ROOT}:/out/r.json:/content", httpx.Response(201, json={}))
    graph.on(
        "POST",
        f"{ROOT}:/out/big.bin:/createUploadSession",
        httpx.Response(200, json={"uploadUrl": "https://upload.example/session/1"}),
    )
    graph.on("PUT", "/session/1", httpx.Response(202, json={}))
    graph.on("DELETE", f"{ROOT}:/out/r.json:", httpx.Response(204))
    connector = graph.connector()

    await connector.write("out/r.json", b"{}", "application/json")
    put = graph.requests[-1]
    assert put.headers["Content-Type"] == "application/json"

    monkeypatch.setattr("app.connectors.sharepoint.SIMPLE_UPLOAD_MAX", 10)
    big = b"x" * (UPLOAD_CHUNK + 5)
    await connector.write("out/big.bin", big, "application/octet-stream")
    chunks = [r for r in graph.requests if r.url.host == "upload.example"]
    assert [r.headers["Content-Range"] for r in chunks] == [
        f"bytes 0-{UPLOAD_CHUNK - 1}/{len(big)}",
        f"bytes {UPLOAD_CHUNK}-{len(big) - 1}/{len(big)}",
    ]
    # The pre-authenticated upload URL never gets our token.
    assert all("Authorization" not in r.headers for r in chunks)

    await connector.delete("out/r.json")
    await connector.aclose()


async def test_signed_url_is_graphs_download_url_and_uploads_are_unsupported() -> None:
    graph = FakeGraph()
    graph.on(
        "GET",
        f"{ROOT}:/a.jpg:",
        httpx.Response(200, json={"@microsoft.graph.downloadUrl": "https://dl.example/a?tok"}),
    )
    connector = graph.connector()
    assert await connector.signed_url("a.jpg") == "https://dl.example/a?tok"
    with pytest.raises(UnsupportedOperation):
        await connector.signed_url("a.jpg", write=True)
    await connector.aclose()


async def test_check_reports_the_drive_or_why_not() -> None:
    graph = FakeGraph()
    graph.on(
        "GET",
        "/v1.0/drives/b%21drive",
        httpx.Response(200, json={"name": "Documents", "driveType": "documentLibrary"}),
    )
    ok = await graph.connector().check()
    assert ok.ok and ok.messages == ["drive 'Documents' (documentLibrary) is reachable"]

    broken = await FakeGraph().connector().check()
    assert not broken.ok


@pytest.mark.parametrize(
    ("config", "secret", "message"),
    [
        ({}, "s", "requires 'drive_id'"),
        ({"drive_id": "d", "identity_type": "account_key"}, "s", "must be one of"),
        ({"drive_id": "d", "identity_type": "service_principal"}, "s", "tenant_id, client_id"),
    ],
)
def test_build_validates(config: dict[str, Any], secret: str, message: str) -> None:
    with pytest.raises(ConnectorConfigError, match=message):
        build_connector("sharepoint", config, secret)


def test_build_service_principal() -> None:
    connector = build_connector(
        "sharepoint",
        {
            "drive_id": "d",
            "identity_type": "service_principal",
            "tenant_id": "t",
            "client_id": "c",
        },
        "secret",
    )
    assert isinstance(connector, SharePointConnector)


# --- redirects on pre-authenticated URLs (SEC-4) ---------------------------------


@pytest.fixture
def _dns(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import socket

    names = {"upload.example": "93.184.216.34", "other.example": "93.184.216.35"}

    async def getaddrinfo(_loop: object, host: str, port: int, **_: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (names.get(host, host), port))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", getaddrinfo)


async def _big_write(location: str) -> tuple[FakeGraph, SharePointConnector]:
    graph = FakeGraph()
    graph.on(
        "POST",
        f"{ROOT}:/out/big.bin:/createUploadSession",
        httpx.Response(200, json={"uploadUrl": "https://upload.example/session/1"}),
    )
    graph.on("PUT", "/session/1", httpx.Response(307, headers={"Location": location}))
    return graph, graph.connector()


@pytest.mark.usefixtures("_dns")
@pytest.mark.parametrize(
    "location",
    [
        "https://169.254.169.254/latest",
        "https://10.0.0.8/x",
        "http://other.example/x",  # not https
    ],
)
async def test_an_upload_url_redirect_to_internal_or_plain_http_is_refused(
    monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    monkeypatch.setattr("app.connectors.sharepoint.SIMPLE_UPLOAD_MAX", 10)
    graph, connector = await _big_write(location)
    with pytest.raises(ConnectorError, match="redirect refused"):
        await connector.write("out/big.bin", b"x" * 20, "application/octet-stream")
    assert [
        r.url.host
        for r in graph.requests
        if r.method == "PUT" and r.url.host != "graph.microsoft.com"
    ] == ["upload.example"]
    await connector.aclose()


@pytest.mark.usefixtures("_dns")
async def test_an_upload_url_may_redirect_to_another_public_https_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.connectors.sharepoint.SIMPLE_UPLOAD_MAX", 10)
    graph, connector = await _big_write("https://other.example/session/2")
    graph.on("PUT", "/session/2", httpx.Response(202, json={}))
    await connector.write("out/big.bin", b"x" * 20, "application/octet-stream")
    assert graph.requests[-1].url.host == "other.example"
    await connector.aclose()
