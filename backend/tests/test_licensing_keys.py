"""Tests for Ed25519 licence keys and offline verification (LIC-1).

Covers `services/licensing/license.py` (parsing, verification, status) and
`services/licensing/issue.py` (key generation, signing, the CLI). Real
vendor keys in `services/licensing/keys.py` are never used here — every
test supplies its own key pair via `public_keys=` or a monkeypatch.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from datetime import date
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.services.licensing import issue as issue_mod
from app.services.licensing.issue import generate_keypair, issue, main
from app.services.licensing.keys import VENDOR_PUBLIC_KEYS
from app.services.licensing.license import (
    PREFIX,
    InvalidLicenseError,
    License,
    LicenseStatus,
    b64url_decode,
    b64url_encode,
    license_status,
    parse_and_verify,
)

#: The production mapping as `keys.py` defines it, copied at import, before the
#: `_unkeyed_build` fixture empties the live dict for each test.
PRODUCTION_KEYS = dict(VENDOR_PUBLIC_KEYS)

KID = "vtest"


@pytest.fixture
def keypair() -> tuple[bytes, bytes]:
    return generate_keypair()


def _payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "v": 1,
        "kid": KID,
        "lic": "11111111-1111-1111-1111-111111111111",
        "tier": "commercial",
        "licensee": "Acme Oy",
        "seats": 5,
        "issued_at": "2026-01-01",
        "expires_at": "2027-01-01",
        "features": ["sso"],
    }
    base.update(overrides)
    return base


def _make_key(private_key: bytes, **overrides: object) -> str:
    return issue(private_key, _payload(**overrides))


class TestRoundTrip:
    def test_valid_key_verifies(self, keypair: tuple[bytes, bytes]) -> None:
        private_key, public_key = keypair
        key = _make_key(private_key)

        license_ = parse_and_verify(key, public_keys={KID: public_key})

        # A key signed with the old "commercial" tier reads as "team" (alias).
        assert license_ == License(
            license_id="11111111-1111-1111-1111-111111111111",
            tier="team",
            licensee="Acme Oy",
            seats=5,
            issued_at=date(2026, 1, 1),
            expires_at=date(2027, 1, 1),
            features=("sso",),
            kid=KID,
        )

    def test_wire_format(self, keypair: tuple[bytes, bytes]) -> None:
        private_key, _public_key = keypair
        key = _make_key(private_key)
        parts = key.split(".")
        assert len(parts) == 3
        assert parts[0] == PREFIX

    def test_padding_is_tolerated(self, keypair: tuple[bytes, bytes]) -> None:
        """A key whose segments carry standard base64 padding still verifies.

        The signature covers the segment exactly as transmitted, so this
        signs a deliberately-padded payload segment directly, rather than
        padding a segment that was already signed unpadded (which would
        change the signed message and break the signature).
        """
        private_key, public_key = keypair
        # One byte of payload guarantees standard base64 padding ("==").
        payload_segment = base64.urlsafe_b64encode(b"x").decode("ascii")
        assert payload_segment.endswith("=="), "fixture should exercise the padded case"
        message = f"{PREFIX}.{payload_segment}".encode("ascii")
        signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(message)
        sig_segment = base64.urlsafe_b64encode(signature).decode("ascii")
        assert sig_segment.endswith("="), "Ed25519 signatures (64 bytes) always pad"
        key = f"{PREFIX}.{payload_segment}.{sig_segment}"

        # The payload itself ("x") is not a JSON object, so this exercises
        # decoding of padded segments up to (but not through) verification —
        # a malformed payload after a successful, padding-tolerant decode.
        with pytest.raises(InvalidLicenseError, match="JSON"):
            parse_and_verify(key, public_keys={KID: public_key})

    def test_full_key_with_padded_segments_verifies(self, keypair: tuple[bytes, bytes]) -> None:
        """A genuine, valid licence whose base64 segments happen to need padding."""
        private_key, public_key = keypair
        payload = _payload()
        payload_bytes = b""
        for filler in ("", "a", "aa"):  # one of these forces len % 3 != 0 -> real padding
            payload["padding_filler"] = filler
            candidate = json.dumps(payload, sort_keys=True).encode("utf-8")
            if len(candidate) % 3 != 0:
                payload_bytes = candidate
                break
        assert payload_bytes, "expected at least one filler to force padding"

        payload_segment = base64.urlsafe_b64encode(payload_bytes).decode("ascii")
        assert payload_segment.endswith("="), "fixture should exercise the padded case"
        message = f"{PREFIX}.{payload_segment}".encode("ascii")
        signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(message)
        sig_segment = base64.urlsafe_b64encode(signature).decode("ascii")
        key = f"{PREFIX}.{payload_segment}.{sig_segment}"

        license_ = parse_and_verify(key, public_keys={KID: public_key})
        assert license_.licensee == "Acme Oy"


def _pad(segment: str) -> str:
    return segment + "=" * (-len(segment) % 4)


class TestB64UrlDecode:
    def test_accepts_both_padded_and_unpadded(self) -> None:
        unpadded = b64url_encode(b"hi")
        assert unpadded == "aGk"
        assert b64url_decode(unpadded) == b"hi"
        assert b64url_decode(unpadded + "=") == b"hi"


class TestTampering:
    def test_tampered_payload_is_rejected(self, keypair: tuple[bytes, bytes]) -> None:
        private_key, public_key = keypair
        key = _make_key(private_key)
        prefix, payload_segment, sig_segment = key.split(".")

        tampered_payload = json.loads(base64.urlsafe_b64decode(_pad(payload_segment)))
        tampered_payload["seats"] = 999
        tampered_segment = b64url_encode(json.dumps(tampered_payload).encode("utf-8"))
        tampered_key = f"{prefix}.{tampered_segment}.{sig_segment}"

        with pytest.raises(InvalidLicenseError):
            parse_and_verify(tampered_key, public_keys={KID: public_key})

    def test_tampered_signature_is_rejected(self, keypair: tuple[bytes, bytes]) -> None:
        private_key, public_key = keypair
        key = _make_key(private_key)
        prefix, payload_segment, sig_segment = key.split(".")
        flipped = ("A" if sig_segment[0] != "A" else "B") + sig_segment[1:]
        tampered_key = f"{prefix}.{payload_segment}.{flipped}"

        with pytest.raises(InvalidLicenseError):
            parse_and_verify(tampered_key, public_keys={KID: public_key})

    def test_unknown_kid_is_rejected(self, keypair: tuple[bytes, bytes]) -> None:
        private_key, _public_key = keypair
        key = _make_key(private_key)

        with pytest.raises(InvalidLicenseError):
            parse_and_verify(key, public_keys={"other-kid": os.urandom(32)})

    def test_wrong_prefix_is_rejected(self, keypair: tuple[bytes, bytes]) -> None:
        private_key, public_key = keypair
        key = _make_key(private_key)
        _prefix, payload_segment, sig_segment = key.split(".")
        bad_key = f"WRONG.{payload_segment}.{sig_segment}"

        with pytest.raises(InvalidLicenseError):
            parse_and_verify(bad_key, public_keys={KID: public_key})

    def test_bad_version_is_rejected(self, keypair: tuple[bytes, bytes]) -> None:
        private_key, public_key = keypair
        key = _make_key(private_key, v=2)

        with pytest.raises(InvalidLicenseError):
            parse_and_verify(key, public_keys={KID: public_key})

    def test_malformed_key_is_rejected(self) -> None:
        with pytest.raises(InvalidLicenseError):
            parse_and_verify("not-a-licence-key")

    def test_bad_base64_is_rejected(self) -> None:
        with pytest.raises(InvalidLicenseError):
            parse_and_verify(f"{PREFIX}.not base64!!.also-not-base64!!")

    def test_bad_json_is_rejected(self, keypair: tuple[bytes, bytes]) -> None:
        private_key, public_key = keypair
        payload_segment = b64url_encode(b"not json")
        message = f"{PREFIX}.{payload_segment}".encode("ascii")
        signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(message)
        sig_segment = b64url_encode(signature)
        key = f"{PREFIX}.{payload_segment}.{sig_segment}"

        with pytest.raises(InvalidLicenseError):
            parse_and_verify(key, public_keys={KID: public_key})

    def test_payload_that_is_not_text_is_rejected(self, keypair: tuple[bytes, bytes]) -> None:
        # "nope" is valid base64url for bytes that are not UTF-8.
        _, public_key = keypair
        with pytest.raises(InvalidLicenseError, match="JSON"):
            parse_and_verify(f"{PREFIX}.nope.nope", public_keys={KID: public_key})

    @pytest.mark.parametrize("tier", ["personal", "free", "", None])
    def test_bad_tier_is_rejected(self, keypair: tuple[bytes, bytes], tier: object) -> None:
        private_key, public_key = keypair
        key = _make_key(private_key, tier=tier)

        with pytest.raises(InvalidLicenseError):
            parse_and_verify(key, public_keys={KID: public_key})

    @pytest.mark.parametrize("seats", [0, -1, "5"])
    def test_bad_seats_is_rejected(self, keypair: tuple[bytes, bytes], seats: object) -> None:
        private_key, public_key = keypair
        key = _make_key(private_key, seats=seats)

        with pytest.raises(InvalidLicenseError):
            parse_and_verify(key, public_keys={KID: public_key})

    def test_missing_field_is_rejected(self, keypair: tuple[bytes, bytes]) -> None:
        private_key, public_key = keypair
        payload = _payload()
        del payload["licensee"]
        key = issue(private_key, payload)

        with pytest.raises(InvalidLicenseError):
            parse_and_verify(key, public_keys={KID: public_key})

    def test_unknown_fields_are_ignored(self, keypair: tuple[bytes, bytes]) -> None:
        private_key, public_key = keypair
        key = _make_key(private_key, extra_future_field="ignored")

        license_ = parse_and_verify(key, public_keys={KID: public_key})
        assert license_.licensee == "Acme Oy"


class TestLicenseStatus:
    def test_no_key_is_community(self) -> None:
        assert license_status(None) == (LicenseStatus.COMMUNITY, None)
        assert license_status("") == (LicenseStatus.COMMUNITY, None)

    def test_valid_key_is_valid(self, keypair: tuple[bytes, bytes]) -> None:
        private_key, public_key = keypair
        key = _make_key(private_key)

        status, license_ = license_status(
            key, today=date(2026, 6, 1), public_keys={KID: public_key}
        )
        assert status == LicenseStatus.VALID
        assert license_ is not None
        assert license_.tier == "team"

    def test_expired_key(self, keypair: tuple[bytes, bytes]) -> None:
        private_key, public_key = keypair
        key = _make_key(private_key)

        status, license_ = license_status(
            key, today=date(2028, 1, 1), public_keys={KID: public_key}
        )
        assert status == LicenseStatus.EXPIRED
        assert license_ is not None

    def test_invalid_key_is_invalid(self) -> None:
        status, license_ = license_status("garbage", public_keys={})
        assert status == LicenseStatus.INVALID
        assert license_ is None


class TestVendorKeysModule:
    def test_production_keys_are_ed25519_public_keys(self) -> None:
        assert PRODUCTION_KEYS, "keys.py must carry the production public key"
        for kid, public_key in PRODUCTION_KEYS.items():
            assert len(public_key) == 32
            Ed25519PublicKey.from_public_bytes(public_key)
            # The kid the keypair command suggests: "v" + the key's first 8 hex digits.
            assert kid == f"v{public_key.hex()[:8]}"

    def test_tests_run_as_an_unkeyed_build(self) -> None:
        assert VENDOR_PUBLIC_KEYS == {}


class TestCli:
    def test_keypair_writes_0600_and_refuses_overwrite(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out_path = str(tmp_path / "vendor.key")
        main(["keypair", "--out", out_path])
        captured = capsys.readouterr()
        assert "Public key (hex)" in captured.out
        assert "Suggested kid" in captured.out

        mode = stat.S_IMODE(os.stat(out_path).st_mode)
        assert mode == 0o600

        with pytest.raises(SystemExit):
            main(["keypair", "--out", out_path])

    def test_sign_output_verifies(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        out_path = str(tmp_path / "vendor2.key")
        main(["keypair", "--out", out_path])
        keypair_output = capsys.readouterr().out
        public_hex = next(
            line.split(": ", 1)[1]
            for line in keypair_output.splitlines()
            if line.startswith("Public key (hex)")
        )
        public_key = bytes.fromhex(public_hex)
        kid = next(
            line.split(": ", 1)[1]
            for line in keypair_output.splitlines()
            if line.startswith("Suggested kid")
        )

        main(
            [
                "sign",
                "--private-key",
                out_path,
                "--kid",
                kid,
                "--licensee",
                "Acme Oy",
                "--tier",
                "enterprise",
                "--seats",
                "42",
                "--expires",
                "2030-01-01",
                "--feature",
                "sso",
                "--feature",
                "audit-log",
            ]
        )
        signed_output = capsys.readouterr().out.strip()

        # Private key material never reaches stdout.
        private_key = issue_mod._read_private_key(out_path)
        assert private_key.hex() not in keypair_output
        assert private_key.hex() not in signed_output

        license_ = parse_and_verify(signed_output, public_keys={kid: public_key})
        assert license_.tier == "enterprise"
        assert license_.seats == 42
        assert license_.licensee == "Acme Oy"
        assert set(license_.features) == {"sso", "audit-log"}
