"""Reverse-proxy header trust (SEC-3): `APP_TRUSTED_PROXIES` and `ProxyHeadersMiddleware`."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.deps import ClientIpDep
from app.core.config import Settings, get_settings
from app.core.proxy import ProxyHeadersMiddleware, parse_trusted_proxies
from app.main import create_app

PROXY = "10.0.0.1"
CLIENT = "203.0.113.7"


def _probe_app(trusted: list[str]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(ProxyHeadersMiddleware, trusted_proxies=trusted)

    @app.get("/probe")
    async def probe(request: Request, client_ip: ClientIpDep) -> dict[str, str | None]:
        return {"ip": client_ip, "scheme": request.url.scheme}

    return app


def _get(
    app: FastAPI, *, peer: str, headers: dict[str, str] | None = None
) -> dict[str, str | None]:
    with TestClient(app, client=(peer, 4242)) as client:
        response = client.get("/probe", headers=headers or {})
    assert response.status_code == 200
    data: dict[str, str | None] = response.json()
    return data


# --- Middleware ----------------------------------------------------------------


def test_forwarded_headers_ignored_without_trusted_proxies() -> None:
    app = _probe_app([])
    data = _get(app, peer=PROXY, headers={"X-Forwarded-For": CLIENT, "X-Forwarded-Proto": "https"})
    assert data == {"ip": PROXY, "scheme": "http"}


def test_forwarded_headers_ignored_from_untrusted_peer() -> None:
    app = _probe_app([PROXY])
    data = _get(
        app, peer="192.0.2.9", headers={"X-Forwarded-For": CLIENT, "X-Forwarded-Proto": "https"}
    )
    assert data == {"ip": "192.0.2.9", "scheme": "http"}


def test_trusted_peer_rewrites_client_and_scheme() -> None:
    app = _probe_app([PROXY])
    data = _get(app, peer=PROXY, headers={"X-Forwarded-For": CLIENT, "X-Forwarded-Proto": "https"})
    assert data == {"ip": CLIENT, "scheme": "https"}


def test_cidr_matches_whole_proxy_subnet() -> None:
    app = _probe_app(["10.0.0.0/8"])
    data = _get(app, peer="10.20.30.40", headers={"X-Forwarded-For": CLIENT})
    assert data["ip"] == CLIENT


def test_chain_skips_trusted_hops_right_to_left() -> None:
    """A forged leftmost entry is ignored; the first untrusted hop wins."""
    app = _probe_app(["10.0.0.0/8"])
    data = _get(
        app,
        peer=PROXY,
        headers={"X-Forwarded-For": f"1.2.3.4, {CLIENT}, 10.0.0.2"},
    )
    assert data["ip"] == CLIENT


def test_all_trusted_chain_falls_back_to_leftmost() -> None:
    app = _probe_app(["10.0.0.0/8"])
    data = _get(app, peer=PROXY, headers={"X-Forwarded-For": "10.0.0.3, 10.0.0.2"})
    assert data["ip"] == "10.0.0.3"


def test_trusted_peer_without_headers_keeps_peer() -> None:
    app = _probe_app([PROXY])
    data = _get(app, peer=PROXY)
    assert data == {"ip": PROXY, "scheme": "http"}


def test_unknown_forwarded_proto_is_ignored() -> None:
    app = _probe_app([PROXY])
    data = _get(app, peer=PROXY, headers={"X-Forwarded-Proto": "gopher"})
    assert data["scheme"] == "http"


def test_ipv6_proxy() -> None:
    app = _probe_app(["fd00::/8"])
    data = _get(app, peer="fd00::1", headers={"X-Forwarded-For": "2001:db8::9"})
    assert data["ip"] == "2001:db8::9"


# --- Settings ------------------------------------------------------------------


def test_parse_trusted_proxies_accepts_ips_and_cidrs() -> None:
    networks = parse_trusted_proxies(["10.0.0.1", " 192.168.0.0/16 "])
    assert [str(network) for network in networks] == ["10.0.0.1/32", "192.168.0.0/16"]


def test_settings_split_and_validate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_TRUSTED_PROXIES", "10.0.0.1, 192.168.0.0/16")
    assert Settings().trusted_proxies == ["10.0.0.1", "192.168.0.0/16"]

    monkeypatch.setenv("APP_TRUSTED_PROXIES", "")
    assert Settings().trusted_proxies == []

    monkeypatch.setenv("APP_TRUSTED_PROXIES", "not-an-ip")
    with pytest.raises(ValueError, match="APP_TRUSTED_PROXIES"):
        Settings()


def test_create_app_installs_middleware_only_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def installed(app: FastAPI) -> bool:
        return any(m.cls is ProxyHeadersMiddleware for m in app.user_middleware)

    monkeypatch.delenv("APP_TRUSTED_PROXIES", raising=False)
    get_settings.cache_clear()
    assert not installed(create_app())

    monkeypatch.setenv("APP_TRUSTED_PROXIES", PROXY)
    get_settings.cache_clear()
    assert installed(create_app())
    get_settings.cache_clear()
