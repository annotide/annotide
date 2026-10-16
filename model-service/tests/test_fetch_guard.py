"""Caller-supplied media URLs may not reach cloud metadata endpoints or, when
`MODEL_FETCH_PRIVATE_NETWORKS` is set, private addresses outside it."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.backends.heuristic import fetch_image_bytes, resolve_media_url


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://169.254.169.254:80/metadata/instance?api-version=2021-02-01",
        "http://[fd00:ec2::254]/latest/meta-data/",
        "http://[fe80::1]/",
        "http://[::ffff:169.254.169.254]/latest/meta-data/",
        "http://100.100.100.200/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://METADATA.google.internal./computeMetadata/v1/",
    ],
)
async def test_metadata_endpoints_are_refused(url: str) -> None:
    with pytest.raises(ValueError, match="metadata"):
        await resolve_media_url(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/a.png", "http:///a.png"])
async def test_only_http_urls_with_a_host(url: str) -> None:
    with pytest.raises(ValueError, match="http"):
        await resolve_media_url(url)


async def test_a_name_resolving_to_metadata_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(*_args: Any, **_kwargs: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 80))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)

    with pytest.raises(ValueError, match="metadata"):
        await resolve_media_url("http://innocent.example/a.png")


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:8000/api/v1/storage/local/x/a.png",  # in-cluster backend
        "http://172.18.0.4/a.png",
        "http://127.0.0.1:8000/a.png",
        "https://203.0.113.7/a.png",
    ],
)
async def test_private_and_public_addresses_stay_allowed(url: str) -> None:
    await resolve_media_url(url)


async def test_fetch_refuses_before_connecting() -> None:
    with pytest.raises(ValueError, match="metadata"):
        await fetch_image_bytes("http://169.254.169.254/latest/meta-data/")


def _resolve_to(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> list[str]:
    """Answer every name lookup with `addresses`; return the names looked up.

    IP literals resolve to themselves, as the real resolver does.
    """
    looked_up: list[str] = []

    async def resolve(host: str, port: int, **_kwargs: Any) -> list[Any]:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            looked_up.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (a, port)) for a in addresses]
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (host, port))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    return looked_up


async def test_one_bad_address_among_several_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolve_to(monkeypatch, "203.0.113.7", "169.254.169.254")

    with pytest.raises(ValueError, match="metadata"):
        await resolve_media_url("http://innocent.example/a.png")


@pytest.mark.parametrize(
    ("networks", "url", "allowed"),
    [
        ("none", "http://10.0.0.5/a.png", False),
        ("none", "http://127.0.0.1/a.png", False),
        ("none", "http://[::1]/a.png", False),
        ("none", "https://8.8.8.8/a.png", True),
        ("10.0.0.0/8", "http://10.0.0.5:8000/a.png", True),
        ("10.0.0.0/8", "http://172.18.0.4/a.png", False),
        ("10.0.0.0/8, 172.16.0.0/12", "http://172.18.0.4/a.png", True),
        ("10.0.0.0/8", "https://8.8.8.8/a.png", True),
        ("*", "http://127.0.0.1/a.png", True),
    ],
)
async def test_private_networks_allow_list(
    monkeypatch: pytest.MonkeyPatch, networks: str, url: str, allowed: bool
) -> None:
    monkeypatch.setenv("MODEL_FETCH_PRIVATE_NETWORKS", networks)

    if allowed:
        await resolve_media_url(url)
    else:
        with pytest.raises(ValueError, match="MODEL_FETCH_PRIVATE_NETWORKS"):
            await resolve_media_url(url)


async def test_listing_a_metadata_range_does_not_open_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_FETCH_PRIVATE_NETWORKS", "169.254.0.0/16")

    with pytest.raises(ValueError, match="metadata"):
        await resolve_media_url("http://169.254.169.254/")


async def test_an_invalid_allow_list_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_FETCH_PRIVATE_NETWORKS", "10.0.0.0/8,not-a-cidr")

    with pytest.raises(ValueError, match="MODEL_FETCH_PRIVATE_NETWORKS is invalid"):
        await resolve_media_url("http://10.0.0.5/a.png")


@pytest.fixture
async def media_server() -> AsyncIterator[tuple[int, list[bytes]]]:
    """A one-route HTTP server on 127.0.0.1; yields its port and the request heads."""
    heads: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        heads.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nimage")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        yield server.sockets[0].getsockname()[1], heads


async def test_fetch_connects_to_the_checked_address(
    monkeypatch: pytest.MonkeyPatch, media_server: tuple[int, list[bytes]]
) -> None:
    port, heads = media_server
    looked_up = _resolve_to(monkeypatch, "127.0.0.1")

    assert await fetch_image_bytes(f"http://media.example:{port}/a.png?sig=x%2F") == b"image"

    # Resolved once, by the guard: httpx gets the address, not the name, so a
    # second lookup cannot answer differently (DNS rebinding).
    assert looked_up == ["media.example"]
    request_line, *header_lines = heads[0].decode().split("\r\n")
    assert request_line == "GET /a.png?sig=x%2F HTTP/1.1"
    assert f"Host: media.example:{port}" in header_lines
