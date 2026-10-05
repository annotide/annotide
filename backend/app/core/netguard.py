"""Outbound-destination guard shared by webhooks and the HTTP connectors (SEC-4).

Resolves a URL's host and refuses internal destinations (loopback, private,
link-local incl. cloud metadata, ...), either before a request
(`public_destination`), at connect time (`public_only_transport`, which
defeats DNS rebinding) or on a redirect (`redirect_refusal`). Imports nothing
from `app/`, so both services and connectors can use it.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx

#: The EC2 IPv6 metadata endpoint; fd00:ec2::/32 is ULA, so `is_private` alone
#: would let a private-origin redirect through.
_IPV6_METADATA = ipaddress.ip_address("fd00:ec2::254")


def forbidden_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    for reason, flag in (
        ("unspecified", address.is_unspecified),
        ("loopback", address.is_loopback),
        ("link-local", address.is_link_local),
        ("private", address.is_private),
        ("multicast", address.is_multicast),
        ("reserved", address.is_reserved),
    ):
        if flag:
            return f"{reason} address {address}"
    return None


async def _resolve(url: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address] | str:
    """Every address `url` resolves to, or the reason it may not be called at all."""
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return "not an http(s) URL"
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
        infos = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, port, type=socket.SOCK_STREAM
        )
    except (socket.gaierror, UnicodeError, ValueError):
        return f"cannot resolve {parts.hostname}"
    return [ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos]


def is_metadata_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Link-local (169.254.0.0/16, fe80::/10, incl. 169.254.169.254) or fd00:ec2::254."""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_link_local or address == _IPV6_METADATA


async def public_destination(url: str) -> str | None:
    """Why `url` may not be called, or None when every address it resolves to is public.

    Every resolved address must pass, so a name with one public and one
    internal record is refused too. Cloud metadata (169.254.169.254) is
    link-local and therefore refused.
    """
    resolved = await _resolve(url)
    if isinstance(resolved, str):
        return resolved
    for address in resolved:
        reason = forbidden_address(address)
        if reason is not None:
            return reason
    return None


async def destination_refusal(url: str, *, allow_private: bool) -> str | None:
    """Like `public_destination`, but `allow_private` admits private and loopback hosts.

    Link-local / metadata addresses are refused either way.
    """
    resolved = await _resolve(url)
    if isinstance(resolved, str):
        return resolved
    for address in resolved:
        if is_metadata_address(address):
            return f"link-local address {address}"
        if not allow_private:
            reason = forbidden_address(address)
            if reason is not None:
                return reason
    return None


class RedirectRefusedError(httpx.RequestError):
    """A redirect target the guard refused (an `httpx.HTTPError`, so callers' handlers apply)."""


def redirect_guard(
    *, origin_url: str | None = None, https_only: bool = False
) -> Callable[[httpx.Response], Awaitable[None]]:
    """An httpx `response` event hook that vets a redirect before it is followed.

    The target must be http(s) (https with `https_only`) and may not be
    link-local / metadata. With `origin_url` (a connector's configured base
    URL), a target is additionally refused when it is private or internal
    while the origin is public, so a redirect never moves from a public host to
    an internal one; a deliberately private origin may redirect to private
    hosts. Without `origin_url`, only public targets pass.
    """

    async def hook(response: httpx.Response) -> None:
        if not response.has_redirect_location:
            return
        target = str(response.url.join(response.headers["location"]))
        if https_only and urlsplit(target).scheme != "https":
            raise RedirectRefusedError(f"redirect refused: {target} is not https")
        allow_private = False
        if origin_url is not None:
            origin = await _resolve(origin_url)
            # An origin that does not resolve counts as public: the strict side.
            allow_private = not isinstance(origin, str) and any(
                forbidden_address(address) is not None for address in origin
            )
        reason = await destination_refusal(target, allow_private=allow_private)
        if reason is not None:
            raise RedirectRefusedError(f"redirect refused: {reason}")

    return hook


class _PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    """Dials only public addresses, checked on the address actually connected to (SEC-4).

    `public_destination` resolves the name before the request, and the HTTP
    client would resolve it again to connect: a name that answers with a
    public address first and an internal one second (DNS rebinding) passed
    the check. Here the name is resolved once, every address is checked and
    the socket goes to a checked address. TLS still verifies the hostname,
    because httpcore passes it as the server name after the TCP connect.
    """

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._inner = inner or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            async with asyncio.timeout(timeout):
                infos = await asyncio.get_running_loop().getaddrinfo(
                    host, port, type=socket.SOCK_STREAM
                )
        except (socket.gaierror, UnicodeError, TimeoutError) as exc:
            raise httpcore.ConnectError(f"cannot resolve {host}") from exc
        addresses = [ipaddress.ip_address(str(i[4][0]).split("%", 1)[0]) for i in infos]
        for address in addresses:
            reason = forbidden_address(address)
            if reason is not None:
                raise httpcore.ConnectError(f"destination not allowed: {reason}")
        error: httpcore.ConnectError | None = None
        for address in dict.fromkeys(addresses):
            try:
                return await self._inner.connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.ConnectError as exc:
                error = exc
        raise error or httpcore.ConnectError(f"cannot resolve {host}")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("destination not allowed: unix socket")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


def public_only_transport() -> httpx.AsyncHTTPTransport:
    """An httpx transport that connects only to public addresses (SEC-4).

    Use it with `trust_env=False` on the client: an environment proxy would
    otherwise connect on the transport's behalf and skip the check.
    """
    transport = httpx.AsyncHTTPTransport()
    # httpx has no public hook for the network backend; the pool's attribute
    # is pinned by test_public_only_transport_uses_the_guarded_backend.
    pool = transport._pool
    pool._network_backend = _PublicOnlyBackend(pool._network_backend)
    return transport
