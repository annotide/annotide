"""`core/netguard.py`: the redirect guard (SEC-4) and the address rules it shares."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Any

import httpx
import pytest

from app.core.netguard import (
    RedirectRefusedError,
    destination_refusal,
    is_metadata_address,
    redirect_guard,
)

ADDRESSES = {
    "public.example": "93.184.216.34",
    "internal.example": "10.1.2.3",
    "lab.example": "10.9.9.9",
    "meta.example": "169.254.169.254",
}


@pytest.fixture(autouse=True)
def _dns(monkeypatch: pytest.MonkeyPatch) -> None:

    async def getaddrinfo(_loop: object, host: str, port: int, **_: Any) -> list[Any]:
        if host not in ADDRESSES:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                raise socket.gaierror(host) from None
            address = host
        else:
            address = ADDRESSES[host]
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", getaddrinfo)


def _client(
    origin_url: str | None, *, https_only: bool = False, hops: dict[str, str] | None = None
) -> tuple[httpx.AsyncClient, list[str]]:
    reached: list[str] = []
    redirects = hops or {}

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        reached.append(url)
        if url in redirects:
            return httpx.Response(302, headers={"Location": redirects[url]})
        return httpx.Response(200, content=b"ok")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handle),
        follow_redirects=True,
        event_hooks={"response": [redirect_guard(origin_url=origin_url, https_only=https_only)]},
    )
    return client, reached


@pytest.mark.parametrize(
    "address", ["169.254.169.254", "169.254.0.1", "fe80::1", "fd00:ec2::254", "::ffff:169.254.1.1"]
)
def test_metadata_addresses(address: str) -> None:
    assert is_metadata_address(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", ["10.0.0.1", "93.184.216.34", "fd00::1"])
def test_other_addresses_are_not_metadata(address: str) -> None:
    assert not is_metadata_address(ipaddress.ip_address(address))


async def test_destination_refusal_allows_private_only_on_request() -> None:
    assert await destination_refusal("http://lab.example/x", allow_private=True) is None
    refused = await destination_refusal("http://lab.example/x", allow_private=False)
    assert refused is not None
    assert "private" in refused
    meta = await destination_refusal("http://meta.example/x", allow_private=True)
    assert meta is not None
    assert "link-local" in meta
    assert await destination_refusal("http://[fd00:ec2::254]/x", allow_private=True) is not None
    assert await destination_refusal("http://nowhere.example/x", allow_private=True) is not None


async def test_a_public_origin_may_redirect_to_a_public_host() -> None:
    client, reached = _client(
        "https://public.example/",
        hops={"https://public.example/a": "https://93.184.216.34/b"},
    )
    async with client:
        response = await client.get("https://public.example/a")
    assert response.content == b"ok"
    assert reached[-1] == "https://93.184.216.34/b"


@pytest.mark.parametrize(
    "target", ["http://10.1.2.3/x", "http://internal.example/x", "http://127.0.0.1/x"]
)
async def test_a_public_origin_may_not_redirect_to_a_private_host(target: str) -> None:
    client, reached = _client("https://public.example/", hops={"https://public.example/a": target})
    async with client:
        with pytest.raises(RedirectRefusedError, match="redirect refused"):
            await client.get("https://public.example/a")
    assert reached == ["https://public.example/a"]


@pytest.mark.parametrize(
    "target",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://meta.example/x",
        "http://[fd00:ec2::254]/",
    ],
)
async def test_metadata_is_refused_even_for_a_private_origin(target: str) -> None:
    client, reached = _client("http://lab.example/", hops={"http://lab.example/a": target})
    async with client:
        with pytest.raises(RedirectRefusedError):
            await client.get("http://lab.example/a")
    assert reached == ["http://lab.example/a"]


async def test_a_private_origin_may_redirect_within_private_space() -> None:
    client, reached = _client(
        "http://lab.example/", hops={"http://lab.example/a": "http://10.1.2.3/b"}
    )
    async with client:
        assert (await client.get("http://lab.example/a")).content == b"ok"
    assert reached[-1] == "http://10.1.2.3/b"


async def test_a_relative_redirect_is_resolved_against_the_request() -> None:
    client, reached = _client(
        "https://public.example/", hops={"https://public.example/a": "/moved"}
    )
    async with client:
        assert (await client.get("https://public.example/a")).content == b"ok"
    assert reached[-1] == "https://public.example/moved"


async def test_an_unresolvable_target_is_refused() -> None:
    client, _ = _client(
        "https://public.example/", hops={"https://public.example/a": "https://nowhere.example/"}
    )
    async with client:
        with pytest.raises(RedirectRefusedError, match="cannot resolve"):
            await client.get("https://public.example/a")


async def test_https_only_refuses_http_and_other_schemes() -> None:
    client, reached = _client(
        None, https_only=True, hops={"https://public.example/a": "http://public.example/b"}
    )
    async with client:
        with pytest.raises(RedirectRefusedError, match="not https"):
            await client.get("https://public.example/a")
    assert reached == ["https://public.example/a"]


async def test_without_an_origin_only_public_targets_pass() -> None:
    client, _ = _client(None, hops={"https://public.example/a": "https://internal.example/b"})
    async with client:
        with pytest.raises(RedirectRefusedError):
            await client.get("https://public.example/a")
