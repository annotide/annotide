"""Signed revocation lists (LIC-8).

A licence is revoked — a refund, a chargeback, a leaked key — by a list the
vendor signs with the same Ed25519 keys as licence keys:

    ANNR1.<base64url(payload JSON)>.<base64url(signature)>

with payload ``{v: 1, kid, issued_at, revoked: [{lic, at}]}``. ``at`` is the
licence's last valid day; after it the key takes the ordinary expired path,
grace included (``state.py``). An install that never hears of a revocation is
unaffected, so no check depends on reaching the vendor (LIC-28).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final

from app.services.licensing.keys import REVOKED_LICENSES, VENDOR_PUBLIC_KEYS
from app.services.licensing.license import (
    SCHEMA_VERSION,
    InvalidLicenseError,
    parse_date,
    verify_signed,
)

PREFIX: Final = "ANNR1"


@dataclass(frozen=True, slots=True)
class RevocationList:
    issued_at: date
    #: Licence id → last valid day.
    revoked: Mapping[str, date]


def parse_and_verify(
    token: str, *, public_keys: Mapping[str, bytes] | None = None
) -> RevocationList:
    """Verify a revocation list. Raises :class:`InvalidLicenseError` when unusable."""
    keys = VENDOR_PUBLIC_KEYS if public_keys is None else public_keys
    payload, _kid = verify_signed(token, prefix=PREFIX, public_keys=keys)
    if payload.get("v") != SCHEMA_VERSION:
        raise InvalidLicenseError("Unsupported revocation list version.")
    entries = payload.get("revoked")
    if not isinstance(entries, list):
        raise InvalidLicenseError("A revocation list needs a `revoked` list.")
    revoked: dict[str, date] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("lic"), str):
            raise InvalidLicenseError("Each revocation needs a licence id.")
        at = parse_date(entry.get("at"))
        lic: str = entry["lic"]
        revoked[lic] = min(at, revoked.get(lic, at))
    return RevocationList(issued_at=parse_date(payload.get("issued_at")), revoked=revoked)


def revoked_licences(
    stored: str | None, *, public_keys: Mapping[str, bytes] | None = None
) -> dict[str, date]:
    """The compiled revocations merged with a stored list; the earlier date wins.

    A stored list that no longer verifies (a vendor key was retired) is
    ignored rather than trusted.
    """
    merged = dict(REVOKED_LICENSES)
    if stored:
        try:
            listed = parse_and_verify(stored, public_keys=public_keys)
        except InvalidLicenseError:
            listed = None
        if listed is not None:
            for lic, at in listed.revoked.items():
                merged[lic] = min(at, merged.get(lic, at))
    return merged
