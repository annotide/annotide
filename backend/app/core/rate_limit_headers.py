"""`RateLimit-*` headers on every rate-limited response (API-5).

`api.deps.enforce_rate_limit` records the tightest window a request was
counted against in ``scope["state"]["rate_limit"]`` as ``(limit, remaining,
reset_after)``; this middleware turns it into ``RateLimit-Limit`` /
``RateLimit-Remaining`` / ``RateLimit-Reset`` (seconds) on the way out, so a
client can slow down before it meets a 429. Requests nobody counted (health,
public routes, rate limiting off) get none.
"""

from __future__ import annotations

from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

STATE_KEY = "rate_limit"
HEADER_NAMES = ("RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset")


class RateLimitHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Created here, before any inner middleware might copy the scope, so
        # the dict `request.state` writes into is the one read below.
        scope.setdefault("state", {})

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                state: Any = scope.get("state") or {}
                window = state.get(STATE_KEY) if isinstance(state, dict) else None
                if window is not None:
                    present = {name.lower() for name, _ in message.get("headers", [])}
                    extra = [
                        (name.lower().encode("latin-1"), str(value).encode("latin-1"))
                        for name, value in zip(HEADER_NAMES, window, strict=True)
                        if name.lower().encode("latin-1") not in present
                    ]
                    message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        await self.app(scope, receive, send_with_headers)
