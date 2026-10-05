"""The one place that talks to the vendor licence server (LIC-6, LIC-27).

Only the licence refresh and the heartbeat call it, and neither is in any
request's critical path: a failure is recorded for the admin and nothing else
changes (``docs/LICENSING.md``, "Enforcement without a vendor dependency").
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

import httpx

from app.core.config import Settings

TIMEOUT_SECONDS: Final = 10.0
#: Stored errors are for a settings page, not a log: keep them short.
_MAX_ERROR_LENGTH: Final = 300


def open_client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """A client for the licence server; tests pass an ``httpx.MockTransport``."""
    return httpx.AsyncClient(timeout=TIMEOUT_SECONDS, transport=transport)


async def post(
    client: httpx.AsyncClient, settings: Settings, path: str, payload: Mapping[str, Any]
) -> httpx.Response:
    """POST ``payload`` to ``{APP_LICENSE_SERVER_URL}{path}``; raises on non-2xx."""
    if not settings.license_server_url:
        raise ValueError("no licence server is configured")
    url = settings.license_server_url.rstrip("/") + path
    response = await client.post(url, json=dict(payload))
    response.raise_for_status()
    return response


def describe(exc: Exception) -> str:
    """A short, admin-readable reason a call failed."""
    if isinstance(exc, httpx.HTTPStatusError):
        text = f"The licence server answered HTTP {exc.response.status_code}."
    elif isinstance(exc, httpx.TimeoutException):
        text = "The licence server did not answer in time."
    elif isinstance(exc, httpx.HTTPError):
        text = f"Could not reach the licence server ({type(exc).__name__})."
    else:
        text = str(exc) or type(exc).__name__
    return text[:_MAX_ERROR_LENGTH]
