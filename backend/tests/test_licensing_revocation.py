"""Signed revocation lists (LIC-8): parsing, merging and their effect on the
licence in force. The refresh and `PUT /license` sides are in
`test_licensing_calls.py`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

import pytest

from app.services.licensing import issue as issue_mod
from app.services.licensing import keys as keys_mod
from app.services.licensing.issue import generate_keypair, issue, sign_token
from app.services.licensing.license import InvalidLicenseError, LicenseStatus
from app.services.licensing.revocation import PREFIX, parse_and_verify, revoked_licences
from app.services.licensing.state import KeySource, grace_ends, is_restricted, resolve

TODAY = date(2026, 9, 25)


@pytest.fixture(scope="module")
def signer() -> tuple[bytes, Mapping[str, bytes]]:
    private, public = generate_keypair()
    return private, {"vrev": public}


def revocations(private: bytes, *entries: tuple[str, str], issued_at: str = "2026-09-01") -> str:
    return sign_token(
        private,
        {
            "v": 1,
            "kid": "vrev",
            "issued_at": issued_at,
            "revoked": [{"lic": lic, "at": at} for lic, at in entries],
        },
        prefix=PREFIX,
    )


def licence_key(private: bytes, lic: str = "lic-a") -> str:
    return issue(
        private,
        {
            "v": 1,
            "kid": "vrev",
            "lic": lic,
            "tier": "commercial",
            "licensee": "Acme Oy",
            "seats": 5,
            "issued_at": "2026-01-01",
            "expires_at": "2027-06-30",
            "features": [],
        },
    )


def test_a_list_verifies_and_the_earlier_date_wins(
    signer: tuple[bytes, Mapping[str, bytes]],
) -> None:
    private, public = signer
    token = revocations(
        private, ("lic-a", "2026-10-01"), ("lic-a", "2026-09-10"), ("b", "2026-01-01")
    )
    listed = parse_and_verify(token, public_keys=public)
    assert listed.issued_at == date(2026, 9, 1)
    assert dict(listed.revoked) == {"lic-a": date(2026, 9, 10), "b": date(2026, 1, 1)}


@pytest.mark.parametrize(
    "payload",
    [
        {"v": 2, "kid": "vrev", "issued_at": "2026-09-01", "revoked": []},
        {"v": 1, "kid": "vrev", "issued_at": "2026-09-01", "revoked": "lic-a"},
        {"v": 1, "kid": "vrev", "issued_at": "2026-09-01", "revoked": [{"at": "2026-09-01"}]},
        {"v": 1, "kid": "vrev", "issued_at": "2026-09-01", "revoked": [{"lic": "a"}]},
        {"v": 1, "kid": "vrev", "revoked": []},
    ],
)
def test_malformed_lists_are_refused(
    signer: tuple[bytes, Mapping[str, bytes]], payload: dict[str, object]
) -> None:
    private, public = signer
    with pytest.raises(InvalidLicenseError):
        parse_and_verify(sign_token(private, payload, prefix=PREFIX), public_keys=public)


def test_a_licence_key_is_not_a_revocation_list(
    signer: tuple[bytes, Mapping[str, bytes]],
) -> None:
    private, public = signer
    with pytest.raises(InvalidLicenseError):
        parse_and_verify(licence_key(private), public_keys=public)


def test_compiled_and_stored_revocations_merge(
    signer: tuple[bytes, Mapping[str, bytes]], monkeypatch: pytest.MonkeyPatch
) -> None:
    private, public = signer
    monkeypatch.setitem(keys_mod.REVOKED_LICENSES, "lic-a", date(2026, 8, 1))
    monkeypatch.setitem(keys_mod.REVOKED_LICENSES, "lic-c", date(2026, 7, 1))
    stored = revocations(private, ("lic-a", "2026-09-01"), ("lic-b", "2026-09-02"))

    assert revoked_licences(stored, public_keys=public) == {
        "lic-a": date(2026, 8, 1),
        "lic-b": date(2026, 9, 2),
        "lic-c": date(2026, 7, 1),
    }
    # A stored list that no longer verifies is ignored, not trusted.
    assert revoked_licences(stored, public_keys={}) == {
        "lic-a": date(2026, 8, 1),
        "lic-c": date(2026, 7, 1),
    }
    assert revoked_licences(None) == {"lic-a": date(2026, 8, 1), "lic-c": date(2026, 7, 1)}


def test_a_revoked_key_takes_the_expired_path_from_its_date(
    signer: tuple[bytes, Mapping[str, bytes]],
) -> None:
    private, public = signer
    key = licence_key(private)

    def at(today: date, revoked_on: date) -> tuple[LicenseStatus, date | None, bool]:
        licence = resolve(
            [(KeySource.ENV, key)],
            today=today,
            public_keys=public,
            revoked={"lic-a": revoked_on},
        )
        assert licence.revoked_at == revoked_on
        return licence.status, grace_ends(licence), is_restricted(licence)

    # Revoked from a future day: still valid until then.
    assert at(TODAY, date(2026, 10, 1)) == (LicenseStatus.VALID, date(2026, 10, 31), False)
    # In grace, then restricted.
    assert at(TODAY, date(2026, 9, 1)) == (LicenseStatus.EXPIRED, date(2026, 10, 1), False)
    assert at(TODAY, date(2026, 8, 1)) == (LicenseStatus.EXPIRED, date(2026, 8, 31), True)


def test_another_licence_is_unaffected(signer: tuple[bytes, Mapping[str, bytes]]) -> None:
    private, public = signer
    licence = resolve(
        [(KeySource.ENV, licence_key(private, lic="lic-b"))],
        today=TODAY,
        public_keys=public,
        revoked={"lic-a": date(2026, 1, 1)},
    )
    assert licence.status is LicenseStatus.VALID
    assert licence.revoked_at is None


def test_issue_cli_signs_a_revocation_list(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    private, public = generate_keypair()
    monkeypatch.setattr(issue_mod, "_read_private_key", lambda _path: private)
    issue_mod.main(
        [
            "revoke",
            "--private-key",
            "unused",
            "--kid",
            "vcli",
            "--revoke",
            "lic-a:2026-09-30",
            "--revoke",
            "urn:lic:b:2026-10-01",
        ]
    )
    listed = parse_and_verify(capsys.readouterr().out.strip(), public_keys={"vcli": public})
    assert dict(listed.revoked) == {"lic-a": date(2026, 9, 30), "urn:lic:b": date(2026, 10, 1)}

    with pytest.raises(SystemExit):
        issue_mod.main(["revoke", "--private-key", "p", "--kid", "k", "--revoke", "2026-09-30"])
