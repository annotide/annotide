"""Vendor Ed25519 public keys for offline licence verification (LIC-1).

This dictionary is the entire trust anchor for licence verification. It maps
a vendor signing-key id (``kid``, as embedded in a licence payload) to the
raw 32-byte Ed25519 public key that must have signed it.

Holds the production public key(s), generated with ``python -m
app.services.licensing.issue keypair``; the private halves never leave the
vendor. Tests empty this mapping (``tests/conftest.py``) and add their own
keys, and the local and CI stack can trust a throwaway key instead (``make
dev-licence``, which mounts a generated copy of this file over it, dev only).
This module must never read from
``core/config.py`` or any other runtime configuration — an installation that
could configure its own trusted signer could mint its own licences, which
defeats the entire scheme. Tests monkeypatch this mapping (or pass
``public_keys=`` explicitly to `license.parse_and_verify` /
`license.license_status`) rather than relying on real vendor keys.
"""

from __future__ import annotations

from datetime import date

VENDOR_PUBLIC_KEYS: dict[str, bytes] = {
    # R Squared Data Solutions, production key 1, generated 2026-10-06.
    "vb4929e71": bytes.fromhex("b4929e718e390c28268e911cb79148722aa1fb0df9d3bc8ef2b246635573f5b1"),
}

#: Licences revoked in this build (LIC-8): licence id → last valid day. The
#: licence refresh brings newer revocations as a signed list; this is for
#: installs that never refresh. Trusted because it is compiled in.
REVOKED_LICENSES: dict[str, date] = {}
