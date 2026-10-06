"""Licence key parsing and offline verification (LIC-1).

See ``docs/CONTRACTS.md`` ("Licence key (LIC-1)") for the wire format:

    ANN1.<base64url(payload JSON)>.<base64url(Ed25519 signature)>

The signature covers the literal ASCII bytes ``ANN1.<payload segment>``
exactly as transmitted — not a re-encoding of the decoded payload, so any
canonicalisation bug cannot silently accept a tampered key.

Verification is entirely offline against the vendor public keys compiled
into :mod:`app.services.licensing.keys`; nothing here makes a network call
or reads from configuration.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app.services.licensing.hosts import is_valid_pattern
from app.services.licensing.keys import VENDOR_PUBLIC_KEYS

#: Literal wire-format prefix and current schema version (LIC-1).
PREFIX: Final[str] = "ANN1"
SCHEMA_VERSION: Final[int] = 1

#: Key tiers (LIC-32). `commercial` is what Team was called before 2026-10-02;
#: a key signed with it reads as `team`.
TEAM: Final[str] = "team"
BUSINESS: Final[str] = "business"
ENTERPRISE: Final[str] = "enterprise"
TRIAL: Final[str] = "trial"
_VALID_TIERS: Final[frozenset[str]] = frozenset({TEAM, BUSINESS, ENTERPRISE, TRIAL})
_TIER_ALIASES: Final[dict[str, str]] = {"commercial": TEAM}


class LicenseStatus(StrEnum):
    """Result of :func:`license_status`."""

    COMMUNITY = "community"
    VALID = "valid"
    EXPIRED = "expired"
    INVALID = "invalid"


class InvalidLicenseError(ValueError):
    """The key is malformed, unsigned by a known vendor key, or otherwise unusable.

    Every rejection reason — bad base64, bad JSON, wrong version, a missing
    or mistyped field, an unknown tier, an unknown ``kid``, a bad signature —
    raises this single exception type, so callers cannot branch on *why* a
    key was rejected (there is nothing actionable to do differently).
    """


@dataclass(frozen=True, slots=True)
class License:
    """A verified licence payload (LIC-1)."""

    license_id: str
    tier: str
    licensee: str
    seats: int
    issued_at: date
    expires_at: date
    features: tuple[str, ...]
    kid: str
    #: Hosts the key is bound to (LIC-29); empty for an unbound key.
    hosts: tuple[str, ...] = ()


def b64url_encode(data: bytes) -> str:
    """Base64url without padding, per the LIC-1 wire format."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    """Base64url, tolerating both padded and unpadded input (LIC-1 accepts either)."""
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError) as exc:
        raise InvalidLicenseError("Malformed base64url segment.") from exc


def verify_signed(
    token: str, *, prefix: str, public_keys: Mapping[str, bytes]
) -> tuple[dict[str, Any], str]:
    """Check a ``<prefix>.<payload>.<signature>`` token; return its payload and ``kid``.

    Shared by licence keys and revocation lists (LIC-8). Raises
    :class:`InvalidLicenseError` for anything malformed or unsigned.
    """
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != prefix:
        raise InvalidLicenseError(f"Not an {prefix} token.")
    _prefix, payload_segment, sig_segment = parts

    payload_bytes = b64url_decode(payload_segment)
    signature = b64url_decode(sig_segment)

    try:
        payload = json.loads(payload_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # Base64 of arbitrary bytes decodes fine and then fails as text, not JSON.
        raise InvalidLicenseError("Licence payload is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise InvalidLicenseError("Licence payload must be a JSON object.")

    kid = payload.get("kid")
    if not isinstance(kid, str) or kid not in public_keys:
        raise InvalidLicenseError("Unknown or missing signing key id.")

    # The message is the transmitted segment verbatim, never a re-encoding of
    # the decoded payload: that would let two different byte strings that
    # decode to the same JSON both pass under one signature.
    message = f"{prefix}.{payload_segment}".encode("ascii")
    try:
        Ed25519PublicKey.from_public_bytes(public_keys[kid]).verify(signature, message)
    except InvalidSignature as exc:
        raise InvalidLicenseError("Signature does not match the licence payload.") from exc
    except (ValueError, TypeError) as exc:
        raise InvalidLicenseError("Signature verification failed.") from exc
    return payload, kid


def parse_and_verify(key: str, *, public_keys: Mapping[str, bytes] | None = None) -> License:
    """Verify the signature and structure of a licence key.

    Raises :class:`InvalidLicenseError` for any malformed, unsigned or
    otherwise unusable key. ``public_keys`` defaults to the compiled vendor
    keys; tests pass their own mapping instead of monkeypatching the module.
    """
    keys = VENDOR_PUBLIC_KEYS if public_keys is None else public_keys
    payload, kid = verify_signed(key, prefix=PREFIX, public_keys=keys)
    return _license_from_payload(payload, kid)


def _license_from_payload(payload: dict[str, Any], kid: str) -> License:
    if payload.get("v") != SCHEMA_VERSION:
        raise InvalidLicenseError("Unsupported licence schema version.")

    license_id = payload.get("lic")
    if not isinstance(license_id, str) or not license_id:
        raise InvalidLicenseError("Missing licence id.")

    tier = payload.get("tier")
    if isinstance(tier, str):
        tier = _TIER_ALIASES.get(tier, tier)
    if tier not in _VALID_TIERS:
        raise InvalidLicenseError("Unknown tier.")

    licensee = payload.get("licensee")
    if not isinstance(licensee, str) or not licensee:
        raise InvalidLicenseError("Missing licensee.")

    seats = payload.get("seats")
    if not isinstance(seats, int) or isinstance(seats, bool) or seats < 1:
        raise InvalidLicenseError("Seats must be an integer >= 1.")

    issued_at = parse_date(payload.get("issued_at"))
    expires_at = parse_date(payload.get("expires_at"))

    features_raw = payload.get("features", [])
    if not isinstance(features_raw, list) or not all(isinstance(f, str) for f in features_raw):
        raise InvalidLicenseError("Features must be a list of strings.")

    hosts_raw = payload.get("hosts", [])
    if not isinstance(hosts_raw, list) or not all(
        isinstance(h, str) and is_valid_pattern(h) for h in hosts_raw
    ):
        raise InvalidLicenseError("Hosts must be a list of lowercase hostnames.")

    return License(
        license_id=license_id,
        tier=tier,
        licensee=licensee,
        seats=seats,
        issued_at=issued_at,
        expires_at=expires_at,
        features=tuple(features_raw),
        kid=kid,
        hosts=tuple(hosts_raw),
    )


def parse_date(value: object) -> date:
    if not isinstance(value, str):
        raise InvalidLicenseError("Dates must be ISO-8601 strings.")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidLicenseError("Dates must be ISO-8601 strings.") from exc


def license_status(
    key: str | None,
    *,
    today: date | None = None,
    public_keys: Mapping[str, bytes] | None = None,
) -> tuple[LicenseStatus, License | None]:
    """Community / valid / expired / invalid, per LIC-1. Never raises.

    No key (``None`` or empty) is Community mode, not an error. ``today``
    defaults to the current UTC date; tests pass it explicitly.
    """
    if not key:
        return LicenseStatus.COMMUNITY, None
    try:
        license_ = parse_and_verify(key, public_keys=public_keys)
    except InvalidLicenseError:
        return LicenseStatus.INVALID, None
    effective_today = today if today is not None else datetime.now(UTC).date()
    if license_.expires_at < effective_today:
        return LicenseStatus.EXPIRED, license_
    return LicenseStatus.VALID, license_
