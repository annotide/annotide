"""Vendor-side licence issuing (LIC-1).

This module is the *only* place the private signing key is ever touched.
It never ships to a customer install — it is a tool the vendor runs to
generate a signing key pair and sign licence payloads for customers. The
resulting public key is pasted into :mod:`app.services.licensing.keys` for
every install to verify offline.

Usage::

    python -m app.services.licensing.issue keypair --out vendor.key
    python -m app.services.licensing.issue sign \\
        --private-key vendor.key --kid v1 --licensee "Acme Oy" \\
        --tier commercial --seats 10 --expires 2027-01-01 --feature sso
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from app.services.licensing.hosts import is_valid_pattern
from app.services.licensing.license import PREFIX, SCHEMA_VERSION, b64url_encode
from app.services.licensing.revocation import PREFIX as REVOCATION_PREFIX


def generate_keypair() -> tuple[bytes, bytes]:
    """Return ``(raw_private_seed, raw_public_key)``, each 32 bytes."""
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public_bytes = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private_bytes, public_bytes


def sign_token(private_key: bytes, payload: Mapping[str, object], *, prefix: str) -> str:
    """Sign ``payload`` as ``<prefix>.<payload>.<signature>`` (keys and revocation lists)."""
    payload_bytes = json.dumps(dict(payload), separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_segment = b64url_encode(payload_bytes)
    message = f"{prefix}.{payload_segment}".encode("ascii")
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(message)
    sig_segment = b64url_encode(signature)
    return f"{prefix}.{payload_segment}.{sig_segment}"


def issue(private_key: bytes, payload: Mapping[str, object]) -> str:
    """Sign ``payload`` (already containing every LIC-1 field) and return the licence key."""
    return sign_token(private_key, payload, prefix=PREFIX)


def _write_private_key(path: str, private_bytes: bytes) -> None:
    """Write the raw private key as hex, refusing to clobber an existing file."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        print(f"Refusing to overwrite existing file: {path}", file=sys.stderr)
        raise SystemExit(1) from None
    with os.fdopen(fd, "w") as handle:
        handle.write(private_bytes.hex())
        handle.write("\n")


def _read_private_key(path: str) -> bytes:
    with open(path, encoding="ascii") as handle:
        return bytes.fromhex(handle.read().strip())


def _cmd_keypair(args: argparse.Namespace) -> None:
    private_bytes, public_bytes = generate_keypair()
    _write_private_key(args.out, private_bytes)
    public_hex = public_bytes.hex()
    kid_suggestion = f"v{public_hex[:8]}"
    print(f"Suggested kid: {kid_suggestion}")
    print(f"Public key (hex): {public_hex}")
    print(f"Private key written to: {args.out} (0600)")
    print(
        "Paste the kid and public key above into "
        "app/services/licensing/keys.py's VENDOR_PUBLIC_KEYS. "
        "The private key file is never printed and never committed."
    )


def _cmd_sign(args: argparse.Namespace) -> None:
    private_bytes = _read_private_key(args.private_key)
    # Round-trip through the public key only to fail fast on a corrupt file;
    # the private key material itself is never printed.
    Ed25519PrivateKey.from_private_bytes(private_bytes).public_key().public_bytes(
        Encoding.Raw, PublicFormat.Raw
    )

    payload: dict[str, object] = {
        "v": SCHEMA_VERSION,
        "kid": args.kid,
        "lic": str(uuid4()),
        "tier": args.tier,
        "licensee": args.licensee,
        "seats": args.seats,
        "issued_at": datetime.now(UTC).date().isoformat(),
        "expires_at": args.expires.isoformat(),
        "features": list(args.feature or []),
    }
    if args.host:
        # Unbound keys (no `hosts`) are for offline Enterprise installs (LIC-29).
        payload["hosts"] = list(args.host)
    print(issue(private_bytes, payload))


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_host(value: str) -> str:
    if not is_valid_pattern(value):
        raise argparse.ArgumentTypeError(f"not a lowercase hostname: {value!r}")
    return value


def _cmd_revoke(args: argparse.Namespace) -> None:
    private_bytes = _read_private_key(args.private_key)
    revoked = []
    for entry in args.revoke:
        lic, _, at = entry.rpartition(":")
        if not lic:
            raise SystemExit(f"--revoke wants LICENCE_ID:YYYY-MM-DD, got {entry!r}")
        revoked.append({"lic": lic, "at": _parse_date(at).isoformat()})
    payload: dict[str, object] = {
        "v": SCHEMA_VERSION,
        "kid": args.kid,
        "issued_at": datetime.now(UTC).date().isoformat(),
        "revoked": revoked,
    }
    print(sign_token(private_bytes, payload, prefix=REVOCATION_PREFIX))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app.services.licensing.issue", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    keypair_parser = subparsers.add_parser(
        "keypair", help="Generate a new Ed25519 signing key pair."
    )
    keypair_parser.add_argument(
        "--out", required=True, help="Path to write the raw private key (hex, 0600)."
    )
    keypair_parser.set_defaults(func=_cmd_keypair)

    sign_parser = subparsers.add_parser("sign", help="Sign a licence payload.")
    sign_parser.add_argument("--private-key", required=True, help="Path written by `keypair`.")
    sign_parser.add_argument("--kid", required=True, help="Vendor signing-key id.")
    sign_parser.add_argument("--licensee", required=True, help="Customer name.")
    sign_parser.add_argument("--tier", required=True, choices=["commercial", "enterprise"])
    sign_parser.add_argument("--seats", required=True, type=int)
    sign_parser.add_argument("--expires", required=True, type=_parse_date, metavar="YYYY-MM-DD")
    sign_parser.add_argument(
        "--feature", action="append", help="Repeatable; a feature flag granted by the licence."
    )
    sign_parser.add_argument(
        "--host",
        action="append",
        type=_parse_host,
        help="Repeatable; bind the key to this host (`annotate.acme.com`, `*.acme.com`).",
    )
    sign_parser.set_defaults(func=_cmd_sign)

    revoke_parser = subparsers.add_parser(
        "revoke",
        help="Sign a revocation list (LIC-8). A newer list replaces older ones, so "
        "list every licence that is still revoked.",
    )
    revoke_parser.add_argument("--private-key", required=True, help="Path written by `keypair`.")
    revoke_parser.add_argument("--kid", required=True, help="Vendor signing-key id.")
    revoke_parser.add_argument(
        "--revoke",
        action="append",
        required=True,
        metavar="LICENCE_ID:YYYY-MM-DD",
        help="Repeatable; the licence and its last valid day.",
    )
    revoke_parser.set_defaults(func=_cmd_revoke)

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
