"""The one place that talks to the vendor licence server (LIC-6, LIC-27, LIC-34).

The licence refresh and the heartbeat call it from the worker, the trial and
"Refresh now" from an admin's click. None is in any request's critical path:
a failure is recorded for the admin and nothing else changes
(``docs/LICENSING.md``, "Enforcement without a vendor dependency").

The production licence server scales to zero and its database pauses, so a
cold call can take about a minute (``docs/CONTRACTS.md``, *Licence refresh and
heartbeat*). Worker calls wait that out; a call that timed out or was told
`503` has woken the server, so it is retried after minutes, not an hour.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Final

import httpx

from app.core.config import Settings

#: Worker calls: nothing waits on them, so they outlast a cold start. The
#: container starts in ~25 s and the licence server answers `503` itself if its
#: database is not back in 60 s; the rest is margin for a slow container start.
#: Refresh and heartbeat back to back still fit in the five-minute cron tick.
BACKGROUND_TIMEOUT: Final = httpx.Timeout(120.0, connect=10.0)
#: An admin's click: below the 60 s an ingress usually allows a request.
REQUEST_TIMEOUT: Final = httpx.Timeout(45.0, connect=10.0)

#: After a timeout or a `502`/`503`/`504`: the server is waking up.
QUICK_RETRY: Final = timedelta(minutes=5)
#: That many quick retries in a row, then hourly.
QUICK_RETRIES: Final = 3
#: Any other failure, and a wake-up that did not come.
RETRY_AFTER: Final = timedelta(hours=1)
#: A `Retry-After` beyond this is not believed.
MAX_RETRY_AFTER: Final = timedelta(days=1)
#: The worker ticks on a schedule and stamps an attempt a moment after the tick;
#: without this slack the next tick is a hair early and skipped.
TICK_TOLERANCE: Final = timedelta(minutes=1)

_WAKING_STATUSES: Final = frozenset({502, 503, 504})
#: Stored errors are for a settings page, not a log: keep them short.
_MAX_ERROR_LENGTH: Final = 300


def open_client(
    transport: httpx.AsyncBaseTransport | None = None, *, background: bool = False
) -> httpx.AsyncClient:
    """A client for the licence server; tests pass an ``httpx.MockTransport``."""
    timeout = BACKGROUND_TIMEOUT if background else REQUEST_TIMEOUT
    return httpx.AsyncClient(timeout=timeout, transport=transport)


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


def is_waking(exc: Exception) -> bool:
    """Whether ``exc`` looks like a licence server that is starting up."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _WAKING_STATUSES
    return isinstance(exc, httpx.TimeoutException)


def describe(exc: Exception) -> str:
    """A short, admin-readable reason a call failed."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code in _WAKING_STATUSES:
            text = f"The licence server is starting up (HTTP {code})."
        else:
            text = f"The licence server answered HTTP {code}."
    elif isinstance(exc, httpx.TimeoutException):
        text = "The licence server did not answer in time."
    elif isinstance(exc, httpx.HTTPError):
        text = f"Could not reach the licence server ({type(exc).__name__})."
    else:
        text = str(exc) or type(exc).__name__
    return text[:_MAX_ERROR_LENGTH]


def is_due(
    retry_at: datetime | None,
    succeeded_at: datetime | None,
    interval: timedelta,
    now: datetime,
) -> bool:
    """Due once ``interval`` has passed since the last success and no retry waits."""
    if retry_at is not None and now < _aware(retry_at) - TICK_TOLERANCE:
        return False
    return succeeded_at is None or now - _aware(succeeded_at) >= interval - TICK_TOLERANCE


def next_attempt(exc: Exception, failures: int, now: datetime) -> datetime:
    """When to try again after the ``failures``-th failure in a row (1 = the first)."""
    delay = QUICK_RETRY if is_waking(exc) and failures <= QUICK_RETRIES else RETRY_AFTER
    asked = _retry_after(exc, now)
    if asked is not None:
        delay = max(delay, min(asked, MAX_RETRY_AFTER))
    return now + delay


def _retry_after(exc: Exception, now: datetime) -> timedelta | None:
    """The server's ``Retry-After`` (seconds or an HTTP date), if it sent one."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    value = exc.response.headers.get("retry-after", "").strip()
    if value.isdigit():
        return timedelta(seconds=int(value))
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(_aware(when) - now, timedelta(0))


def _aware(value: datetime) -> datetime:
    # SQLite hands back naive datetimes; every stored timestamp is UTC.
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
