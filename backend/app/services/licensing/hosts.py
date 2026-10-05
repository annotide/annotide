"""Binding a licence key to the hosts users reach the install on (LIC-29).

A key may carry ``hosts``. The install compares them with the host of each
user request (``Host``, or ``X-Forwarded-Host`` from a trusted proxy, which
``core/proxy.py`` has already applied). Loopback always matches, so a local
admin can always get in. See ``docs/LICENSING.md`` ("Binding a key to its
host") and ``docs/CONTRACTS.md`` ("Licence key (LIC-1)").
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from typing import Final

_LABEL: Final = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_HOSTNAME: Final = re.compile(rf"^{_LABEL}(\.{_LABEL})*$")
_WILDCARD: Final = "*."


def is_valid_pattern(pattern: str) -> bool:
    """A lowercase hostname without a port, or ``*.`` followed by one."""
    name = pattern.removeprefix(_WILDCARD)
    return len(name) <= 253 and _HOSTNAME.match(name) is not None


def normalize_host(header: str | None) -> str | None:
    """The hostname of a ``Host`` header value: lowercase, no port, no trailing dot."""
    if not header:
        return None
    value = header.strip().lower()
    if value.startswith("["):
        # IPv6 literal, `[::1]:8000`.
        end = value.find("]")
        return value[1:end] if end > 0 else None
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value.rstrip(".") or None


def is_loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def host_allowed(hosts: Iterable[str], host: str | None) -> bool:
    """Whether a request on ``host`` may use a key bound to ``hosts``.

    An unbound key (no hosts), a request without a host and loopback always
    pass. ``*.acme.com`` matches any subdomain of ``acme.com``, not the apex.
    """
    patterns = tuple(hosts)
    if not patterns or host is None or is_loopback(host):
        return True
    for pattern in patterns:
        if pattern.startswith(_WILDCARD):
            if host.endswith(pattern[1:]):
                return True
        elif host == pattern:
            return True
    return False
