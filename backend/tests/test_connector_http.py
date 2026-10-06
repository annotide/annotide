"""Tests for `connectors/http.py`: the read-only HTTP(S) URL-list connector."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from app.connectors import (
    ConnectorAuthError,
    ConnectorConfigError,
    ConnectorError,
    ConnectorNotFound,
    HTTPConnector,
    UnsupportedOperation,
)

BASE = "https://cdn.example.com/data/"
FILES: dict[str, bytes] = {
    "/data/images/a.jpg": b"0123456789",
    "/data/images/b%20c.png": b"png!",
    "/data/docs/n.txt": b"note",
}
MANIFEST = "# roads\nimages/a.jpg\n\nhttps://cdn.example.com/data/images/b%20c.png\ndocs/n.txt\n"

Handler = Callable[[httpx.Request], httpx.Response]


def _server(
    *,
    manifest: str = MANIFEST,
    head_allowed: bool = True,
    honour_range: bool = True,
    cors: str | None = None,
) -> tuple[Handler, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.raw_path.decode()
        if path == "/data/manifest.txt":
            return httpx.Response(200, text=manifest)
        if path == "/data/secret.jpg":
            return httpx.Response(403)
        body = FILES.get(path)
        if body is None:
            return httpx.Response(404)
        headers = {
            "etag": f'"{len(body)}"',
            "last-modified": "Wed, 21 Oct 2026 07:28:00 GMT",
            "content-type": "image/jpeg",
        }
        if cors:
            headers["access-control-allow-origin"] = cors
        if request.method == "HEAD":
            if not head_allowed:
                return httpx.Response(405)
            return httpx.Response(200, headers={**headers, "content-length": str(len(body))})
        byte_range = request.headers.get("range")
        if byte_range and honour_range:
            first, _, last = byte_range.removeprefix("bytes=").partition("-")
            stop = int(last) + 1 if last else len(body)
            part = body[int(first) : stop]
            content_range = f"bytes {first}-{stop - 1}/{len(body)}"
            return httpx.Response(
                206, content=part, headers={**headers, "content-range": content_range}
            )
        return httpx.Response(200, content=body, headers=headers)

    return handle, seen


def _connector(handler: Handler, **config: object) -> HTTPConnector:
    return HTTPConnector(
        base_url=str(config.pop("base_url", BASE.rstrip("/"))),
        transport=httpx.MockTransport(handler),
        **config,  # type: ignore[arg-type]  # test-only passthrough of keyword config
    )


async def test_list_reads_the_manifest_and_heads_each_file() -> None:
    handler, _ = _server()
    connector = _connector(handler)

    objects = [obj async for obj in connector.list("")]

    assert [o.path for o in objects] == ["images/a.jpg", "images/b c.png", "docs/n.txt"]
    first = objects[0]
    assert first.size_bytes == 10
    assert first.etag == '"10"'
    assert first.content_type == "image/jpeg"
    assert first.last_modified is not None
    assert first.last_modified.year == 2026
    await connector.aclose()


async def test_list_applies_prefix_and_glob_and_skips_missing_files() -> None:
    handler, _ = _server(manifest="images/a.jpg\nimages/gone.jpg\ndocs/n.txt\n")
    connector = _connector(handler)

    assert [o.path async for o in connector.list("images/")] == ["images/a.jpg"]
    assert [o.path async for o in connector.list("", "*.txt")] == ["docs/n.txt"]


async def test_inline_paths_replace_the_manifest() -> None:
    handler, seen = _server()
    connector = _connector(handler, paths=["docs/n.txt"])

    assert [o.path async for o in connector.list("")] == ["docs/n.txt"]
    assert all("manifest" not in str(r.url) for r in seen)


async def test_json_manifest() -> None:
    handler, _ = _server(manifest='["images/a.jpg", "docs/n.txt"]')
    connector = _connector(handler)

    assert [o.path async for o in connector.list("")] == ["images/a.jpg", "docs/n.txt"]


async def test_head_refused_falls_back_to_a_ranged_get() -> None:
    handler, _ = _server(head_allowed=False)
    connector = _connector(handler, paths=["images/a.jpg"])

    (obj,) = [o async for o in connector.list("")]

    assert obj.size_bytes == 10


@pytest.mark.parametrize(
    "manifest",
    [
        "https://evil.example.org/x.jpg\n",
        "../etc/passwd\n",
        "images/../../x\n",
        "[1]",
        '{"not": "a list"}',
    ],
)
async def test_entries_outside_base_url_are_refused(manifest: str) -> None:
    handler, _ = _server(manifest=manifest)
    connector = _connector(handler)

    with pytest.raises(ConnectorConfigError):
        [o async for o in connector.list("")]


async def test_read_whole_and_ranges() -> None:
    handler, _ = _server()
    connector = _connector(handler)

    assert await connector.read("images/a.jpg") == b"0123456789"
    assert await connector.read("images/a.jpg", 2, 5) == b"234"
    assert await connector.read("images/a.jpg", 7) == b"789"
    assert await connector.read("images/a.jpg", 5, 5) == b""
    assert await connector.read("images/b c.png") == b"png!"


async def test_read_slices_when_the_server_ignores_ranges() -> None:
    handler, _ = _server(honour_range=False)
    connector = _connector(handler)

    assert await connector.read("images/a.jpg", 2, 5) == b"234"


async def test_read_errors_map_to_connector_errors() -> None:
    handler, _ = _server()
    connector = _connector(handler)

    with pytest.raises(ConnectorNotFound):
        await connector.read("images/none.jpg")
    with pytest.raises(ConnectorAuthError):
        await connector.read("secret.jpg")
    with pytest.raises(ConnectorConfigError):
        await connector.read("../outside.jpg")


async def test_network_failure_is_a_connector_error() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    connector = _connector(fail)

    with pytest.raises(ConnectorError):
        await connector.read("images/a.jpg")


async def test_signed_url_is_the_object_url_and_writes_are_refused() -> None:
    handler, _ = _server()
    connector = _connector(handler)

    assert await connector.signed_url("images/b c.png") == BASE + "images/b%20c.png"
    with pytest.raises(UnsupportedOperation):
        await connector.signed_url("images/a.jpg", write=True)
    with pytest.raises(UnsupportedOperation):
        await connector.write("x.jpg", b"", "image/jpeg")
    with pytest.raises(UnsupportedOperation):
        await connector.delete("x.jpg")


def test_base_url_must_be_http() -> None:
    for bad in ("ftp://host/x", "file:///etc", "https://host/x?sig=1", "nohost"):
        with pytest.raises(ConnectorConfigError):
            HTTPConnector(base_url=bad)


async def test_check_reports_count_and_cors() -> None:
    handler, _ = _server()
    connector = _connector(handler, frontend_origin="https://app.example.com")

    result = await connector.check()

    assert result.ok is True
    assert result.messages[0] == "3 file(s) listed"
    assert any("CORS" in m for m in result.messages)

    handler, _ = _server(cors="*")
    allowed = _connector(handler, frontend_origin="https://app.example.com")
    assert not any("CORS" in m for m in (await allowed.check()).messages)


async def test_check_fails_on_empty_or_broken_lists() -> None:
    handler, _ = _server(manifest="# nothing\n")
    assert (await _connector(handler).check()).ok is False

    handler, _ = _server(manifest="images/gone.jpg\n")
    result = await _connector(handler).check()
    assert result.ok is False
    assert "not found" in result.messages[-1]

    handler, _ = _server()
    result = await _connector(handler, manifest="missing.txt").check()
    assert result.ok is False


async def test_check_warns_on_plain_http() -> None:
    handler, _ = _server()
    connector = _connector(handler, base_url="http://cdn.example.com/data", paths=["docs/n.txt"])

    result = await connector.check()

    assert any("plain http" in m for m in result.messages)


# --- redirects (SEC-4) -----------------------------------------------------------


@pytest.fixture
def _dns(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import socket
    from typing import Any

    names = {"cdn.example.com": "93.184.216.34", "lab.internal": "10.5.5.5"}

    async def getaddrinfo(_loop: object, host: str, port: int, **_: Any) -> list[Any]:
        address = names.get(host, host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", getaddrinfo)


def _redirecting(location: str, base: str) -> tuple[HTTPConnector, list[str]]:
    reached: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        reached.append(str(request.url))
        if request.url.host in ("cdn.example.com", "lab.internal"):
            return httpx.Response(302, headers={"Location": location})
        return httpx.Response(200, content=b"internal secret")

    connector = HTTPConnector(base_url=base, paths=["a.jpg"], transport=httpx.MockTransport(handle))
    return connector, reached


@pytest.mark.usefixtures("_dns")
@pytest.mark.parametrize(
    "location", ["http://169.254.169.254/latest/meta-data/", "http://10.0.0.8/admin"]
)
async def test_a_public_base_url_cannot_redirect_to_internal_hosts(location: str) -> None:
    connector, reached = _redirecting(location, BASE)
    with pytest.raises(ConnectorError, match="redirect refused"):
        await connector.read("a.jpg")
    assert reached == ["https://cdn.example.com/data/a.jpg"]
    await connector.aclose()


@pytest.mark.usefixtures("_dns")
async def test_a_public_base_url_may_redirect_to_another_public_host() -> None:
    connector, reached = _redirecting("https://93.184.216.35/data/a.jpg", BASE)
    assert await connector.read("a.jpg") == b"internal secret"
    assert reached[-1] == "https://93.184.216.35/data/a.jpg"
    await connector.aclose()


@pytest.mark.usefixtures("_dns")
async def test_a_private_base_url_may_redirect_privately_but_never_to_metadata() -> None:
    connector, reached = _redirecting("http://10.0.0.8/a.jpg", "http://lab.internal/data/")
    assert await connector.read("a.jpg") == b"internal secret"
    assert reached[-1] == "http://10.0.0.8/a.jpg"
    await connector.aclose()

    connector, _ = _redirecting("http://169.254.169.254/x", "http://lab.internal/data/")
    with pytest.raises(ConnectorError, match="link-local"):
        await connector.read("a.jpg")
    await connector.aclose()
