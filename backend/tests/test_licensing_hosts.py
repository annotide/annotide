"""Host binding of licence keys (LIC-29): matching, parsing and resolution.

The database side (the mismatch clock recorded at sign-in) and the
`/license` endpoints are in `test_licensing_state.py`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import RequestHostDep
from app.core.proxy import ProxyHeadersMiddleware
from app.services.licensing import issue as issue_mod
from app.services.licensing.hosts import (
    host_allowed,
    is_loopback,
    is_valid_pattern,
    normalize_host,
)
from app.services.licensing.issue import generate_keypair, issue
from app.services.licensing.license import LicenseStatus, license_status, parse_and_verify
from app.services.licensing.state import KeySource, grace_ends, is_restricted, resolve

TODAY = date(2026, 9, 25)


@pytest.fixture(scope="module")
def signer() -> tuple[bytes, Mapping[str, bytes]]:
    private, public = generate_keypair()
    return private, {"vhost": public}


def _key(
    private: bytes, *, hosts: object = None, expires_at: str = "2099-01-01", lic: str = "h"
) -> str:
    payload: dict[str, object] = {
        "v": 1,
        "kid": "vhost",
        "lic": lic,
        "tier": "commercial",
        "licensee": "Acme Oy",
        "seats": 5,
        "issued_at": "2026-01-01",
        "expires_at": expires_at,
        "features": [],
    }
    if hosts is not None:
        payload["hosts"] = hosts
    return issue(private, payload)


# --- Matching ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Annotate.Acme.com", "annotate.acme.com"),
        ("annotate.acme.com:8443", "annotate.acme.com"),
        ("annotate.acme.com.", "annotate.acme.com"),
        ("[::1]:8000", "::1"),
        ("::1", "::1"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_host(header: str | None, expected: str | None) -> None:
    assert normalize_host(header) == expected


@pytest.mark.parametrize(
    ("pattern", "valid"),
    [
        ("annotate.acme.com", True),
        ("*.acme.com", True),
        ("intranet", True),
        ("Annotate.acme.com", False),
        ("annotate.acme.com:443", False),
        ("*", False),
        ("a.*.com", False),
        ("-bad.acme.com", False),
        ("", False),
    ],
)
def test_is_valid_pattern(pattern: str, valid: bool) -> None:
    assert is_valid_pattern(pattern) is valid


@pytest.mark.parametrize("host", ["localhost", "app.localhost", "127.0.0.1", "127.8.9.1", "::1"])
def test_loopback(host: str) -> None:
    assert is_loopback(host)
    assert host_allowed(["annotate.acme.com"], host)


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("annotate.acme.com", True),
        ("eu.annotate.acme.com", False),
        ("x.staging.acme.com", True),
        ("acme.com", False),
        ("notacme.com", False),
        ("annotate.acme.com.evil.org", False),
        (None, True),
    ],
)
def test_host_allowed(host: str | None, allowed: bool) -> None:
    assert host_allowed(["annotate.acme.com", "*.staging.acme.com"], host) is allowed


def test_unbound_key_matches_any_host() -> None:
    assert host_allowed([], "anything.example.org")


# --- Parsing -------------------------------------------------------------------


def test_hosts_are_parsed(signer: tuple[bytes, Mapping[str, bytes]]) -> None:
    private, public = signer
    licence = parse_and_verify(_key(private, hosts=["*.acme.com"]), public_keys=public)
    assert licence.hosts == ("*.acme.com",)
    assert parse_and_verify(_key(private), public_keys=public).hosts == ()


@pytest.mark.parametrize("hosts", ["acme.com", ["ACME.com"], [1], [None], ["acme.com:80"]])
def test_malformed_hosts_make_the_key_invalid(
    signer: tuple[bytes, Mapping[str, bytes]], hosts: object
) -> None:
    private, public = signer
    status, licence = license_status(_key(private, hosts=hosts), public_keys=public)
    assert status is LicenseStatus.INVALID
    assert licence is None


# --- Resolution ----------------------------------------------------------------


def test_matching_host_is_valid(signer: tuple[bytes, Mapping[str, bytes]]) -> None:
    private, public = signer
    licence = resolve(
        [(KeySource.ENV, _key(private, hosts=["annotate.acme.com"]))],
        today=TODAY,
        public_keys=public,
        host="annotate.acme.com",
    )
    assert licence.status is LicenseStatus.VALID
    assert licence.host_mismatch_since is None


def test_other_host_takes_the_expired_path_from_today(
    signer: tuple[bytes, Mapping[str, bytes]],
) -> None:
    private, public = signer
    licence = resolve(
        [(KeySource.ENV, _key(private, hosts=["annotate.acme.com"]))],
        today=TODAY,
        public_keys=public,
        host="copy.example.org",
    )
    assert licence.status is LicenseStatus.EXPIRED
    assert licence.host_mismatch_since == TODAY
    assert grace_ends(licence) == date(2026, 10, 25)
    assert not is_restricted(licence)


def test_recorded_mismatch_restricts_after_grace(
    signer: tuple[bytes, Mapping[str, bytes]],
) -> None:
    private, public = signer
    licence = resolve(
        [(KeySource.ENV, _key(private, hosts=["annotate.acme.com"]))],
        today=TODAY,
        public_keys=public,
        host="copy.example.org",
        mismatch_since=date(2026, 8, 1),
    )
    assert grace_ends(licence) == date(2026, 8, 31)
    assert is_restricted(licence)


def test_grace_counts_from_expiry_when_that_came_first(
    signer: tuple[bytes, Mapping[str, bytes]],
) -> None:
    private, public = signer
    licence = resolve(
        [(KeySource.ENV, _key(private, hosts=["annotate.acme.com"], expires_at="2026-07-01"))],
        today=TODAY,
        public_keys=public,
        host="copy.example.org",
        mismatch_since=date(2026, 9, 1),
    )
    assert grace_ends(licence) == date(2026, 7, 31)


def test_a_key_for_this_host_wins_over_one_for_another(
    signer: tuple[bytes, Mapping[str, bytes]],
) -> None:
    private, public = signer
    old_domain = _key(private, hosts=["old.acme.com"], expires_at="2099-01-01", lic="old")
    new_domain = _key(private, hosts=["new.acme.com"], expires_at="2027-01-01", lic="new")
    licence = resolve(
        [(KeySource.ENV, old_domain), (KeySource.ADMIN, new_domain)],
        today=TODAY,
        public_keys=public,
        host="new.acme.com",
    )
    assert licence.status is LicenseStatus.VALID
    assert licence.license is not None
    assert licence.license.license_id == "new"


def test_without_a_host_nothing_is_checked(signer: tuple[bytes, Mapping[str, bytes]]) -> None:
    private, public = signer
    licence = resolve(
        [(KeySource.ENV, _key(private, hosts=["annotate.acme.com"]))],
        today=TODAY,
        public_keys=public,
    )
    assert licence.status is LicenseStatus.VALID


# --- Request host and the issuing CLI --------------------------------------------


def _host_probe(trusted: list[str]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(ProxyHeadersMiddleware, trusted_proxies=trusted)

    @app.get("/probe")
    async def probe(host: RequestHostDep) -> dict[str, str | None]:
        return {"host": host}

    return app


def _probe_host(app: FastAPI, peer: str, headers: dict[str, str]) -> str | None:
    with TestClient(app, client=(peer, 4242)) as client:
        value: str | None = client.get("/probe", headers=headers).json()["host"]
    return value


def test_forwarded_host_is_believed_only_from_a_trusted_proxy() -> None:
    headers = {"Host": "backend:8000", "X-Forwarded-Host": "Annotate.acme.com, proxy.internal"}
    assert _probe_host(_host_probe(["10.0.0.1"]), "10.0.0.1", headers) == "annotate.acme.com"
    assert _probe_host(_host_probe(["10.0.0.1"]), "192.0.2.9", headers) == "backend"
    assert _probe_host(_host_probe([]), "10.0.0.1", headers) == "backend"


def test_issue_cli_binds_hosts(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    private, public = generate_keypair()
    monkeypatch.setattr(issue_mod, "_read_private_key", lambda _path: private)
    issue_mod.main(
        [
            "sign",
            "--private-key",
            "unused",
            "--kid",
            "vcli",
            "--licensee",
            "Acme Oy",
            "--tier",
            "commercial",
            "--seats",
            "3",
            "--expires",
            "2099-01-01",
            "--host",
            "annotate.acme.com",
            "--host",
            "*.staging.acme.com",
        ]
    )
    key = capsys.readouterr().out.strip()
    licence = parse_and_verify(key, public_keys={"vcli": public})
    assert licence.hosts == ("annotate.acme.com", "*.staging.acme.com")


def test_issue_cli_refuses_a_bad_host(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        issue_mod.build_parser().parse_args(
            [
                "sign",
                "--private-key",
                "p",
                "--kid",
                "k",
                "--licensee",
                "A",
                "--tier",
                "commercial",
                "--seats",
                "1",
                "--expires",
                "2099-01-01",
                "--host",
                "Acme.com:80",
            ]
        )
    assert "not a lowercase hostname" in capsys.readouterr().err
