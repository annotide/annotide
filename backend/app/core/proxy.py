"""Reverse-proxy header handling (SEC-3).

The API is usually deployed behind a TLS-terminating proxy or load balancer.
The proxy sees the real client; the app sees the proxy. `X-Forwarded-For` and
`X-Forwarded-Proto` carry the original address and scheme, `X-Forwarded-Host`
the host the browser used (LIC-29 host binding), but any caller can send
those headers, so they are honoured only when the direct peer is one of
the operator-listed `APP_TRUSTED_PROXIES`.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable

from starlette.types import ASGIApp, Receive, Scope, Send

type _Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_trusted_proxies(entries: Iterable[str]) -> tuple[_Network, ...]:
    """Turn `APP_TRUSTED_PROXIES` entries (IPs or CIDRs) into networks.

    A bare address becomes a /32 or /128. Raises ``ValueError`` for anything
    that is not an address or network, so a typo fails at startup rather than
    silently trusting nobody.
    """
    return tuple(ipaddress.ip_network(entry.strip(), strict=False) for entry in entries)


def _is_trusted(host: str | None, trusted: tuple[_Network, ...]) -> bool:
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in network for network in trusted)


def _forwarded_client(header: str, trusted: tuple[_Network, ...]) -> str | None:
    """The first address in `X-Forwarded-For`, read right-to-left, that is not a proxy.

    Each proxy appends the address it received the request from, so the
    rightmost entries are the ones our own proxies added and can be believed;
    anything to the left of the first untrusted hop was supplied by the client
    and may be forged. When every entry is a trusted proxy the leftmost one is
    used, which is the best available guess.
    """
    hops = [hop.strip() for hop in header.split(",") if hop.strip()]
    for hop in reversed(hops):
        if not _is_trusted(hop, trusted):
            return hop
    return hops[0] if hops else None


class ProxyHeadersMiddleware:
    """Rewrite ``scope["client"]``, ``scope["scheme"]`` and ``Host`` from forwarded headers.

    Pure ASGI so it runs before routing and so `request.client` / `request.url`
    are already correct for every dependency (audit IP, cookie `Secure` flag,
    licence host binding).
    """

    def __init__(self, app: ASGIApp, trusted_proxies: Iterable[str]) -> None:
        self.app = app
        self.trusted = parse_trusted_proxies(trusted_proxies)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"} and self.trusted:
            client = scope.get("client")
            peer = client[0] if client else None
            if _is_trusted(peer, self.trusted):
                self._apply(scope)
        await self.app(scope, receive, send)

    def _apply(self, scope: Scope) -> None:
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        forwarded_for = headers.get(b"x-forwarded-for")
        if forwarded_for:
            host = _forwarded_client(forwarded_for.decode("latin-1"), self.trusted)
            if host:
                # Port 0: the original port is not forwarded and nothing reads it.
                scope["client"] = (host, 0)
        forwarded_proto = headers.get(b"x-forwarded-proto")
        if forwarded_proto:
            # A chain of proxies may join their values; the first is the client-facing one.
            proto = forwarded_proto.decode("latin-1").split(",")[0].strip().lower()
            if proto in {"http", "https"}:
                if scope["type"] == "websocket":
                    proto = "wss" if proto == "https" else "ws"
                scope["scheme"] = proto
        forwarded_host = headers.get(b"x-forwarded-host")
        if forwarded_host:
            # Like the scheme, the first value is the one the client used.
            host = forwarded_host.split(b",")[0].strip()
            if host:
                scope["headers"] = [
                    (key, value) for key, value in scope["headers"] if key.lower() != b"host"
                ] + [(b"host", host)]
