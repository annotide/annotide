"""Licence enforcement and telemetry. See ``docs/LICENSING.md``."""

from app.services.licensing.fingerprint import (
    PUBLIC_EMAIL_DOMAINS,
    FingerprintSignal,
    OrganizationFingerprint,
    SignalKind,
    build_fingerprint,
    email_domain,
    is_public_email_domain,
    is_reserved_domain,
    is_routable_host,
    network_prefix,
    storage_account,
)

__all__ = [
    "PUBLIC_EMAIL_DOMAINS",
    "FingerprintSignal",
    "OrganizationFingerprint",
    "SignalKind",
    "build_fingerprint",
    "email_domain",
    "is_public_email_domain",
    "is_reserved_domain",
    "is_routable_host",
    "network_prefix",
    "storage_account",
]
