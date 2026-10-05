"""Organisation fingerprint for the licence heartbeat (LIC-16, LIC-17, LIC-21).

Computed inside the customer's installation and sent with the LIC-6 heartbeat.
It lets the licence server notice that several small Community installs
belong to one organisation, without learning anything about the people using them.

Two rules govern everything here:

1. **Organisations, never people.** The inputs are company-level identifiers —
   an email *domain*, an SSO tenant, a cloud account. The local part of an email
   address is discarded before hashing, so no value here can be traced to a
   person even by someone holding the salt.

2. **Hashes, never plaintext.** Every signal leaves as HMAC-SHA256. The server
   matches hash against hash and never learns the domain or the account number.

The salt is per-vendor and shared by all installs on purpose: two installs must
hash ``acme.com`` to the same value or clustering cannot work at all. A
per-install salt would make the fingerprint useless.

Nothing in this module reads annotations, media, item paths or user names
(LIC-9). :func:`build_fingerprint` takes explicit arguments rather than reaching
into the database, so what is collected is visible at the call site and can be
shown to the admin verbatim before it is sent (LIC-21).
"""

from __future__ import annotations

import hmac
import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from typing import Any, Final
from urllib.parse import urlparse

#: Consumer providers. Hashing these would cluster every unrelated hobbyist in
#: the world into one enormous false organisation, so they are dropped entirely
#: — never hashed, never sent (LIC-17). Visible to the admin by design.
PUBLIC_EMAIL_DOMAINS: Final[frozenset[str]] = frozenset(
    {
        "aol.com",
        "gmail.com",
        "googlemail.com",
        "gmx.com",
        "gmx.de",
        "hotmail.co.uk",
        "hotmail.com",
        "hotmail.fr",
        "icloud.com",
        "live.com",
        "mail.com",
        "mail.ru",
        "me.com",
        "outlook.com",
        "pm.me",
        "proton.me",
        "protonmail.com",
        "qq.com",
        "yahoo.co.uk",
        "yahoo.com",
        "yandex.ru",
        "zoho.com",
        # Finnish consumer providers — the first customers are here.
        "elisanet.fi",
        "kolumbus.fi",
        "luukku.com",
        "pp.inet.fi",
        "saunalahti.fi",
        "suomi24.fi",
    }
)


#: Domains reserved for documentation and testing (RFC 2606). ``demo@example.com``
#: is what everyone types into a trial install; hashing it would make every such
#: install one false organisation, exactly like a public provider.
RESERVED_DOMAINS: Final[frozenset[str]] = frozenset({"example.com", "example.net", "example.org"})

#: Top-level names that are never delegated (RFC 2606, RFC 6761, RFC 6762 and
#: ICANN's ``.internal``) plus the de-facto private ones. ``corp.local`` and
#: ``ad.internal`` are Active Directory defaults shared by unrelated companies.
RESERVED_SUFFIXES: Final[frozenset[str]] = frozenset(
    {
        "corp",
        "example",
        "home",
        "home.arpa",
        "internal",
        "invalid",
        "lan",
        "local",
        "localdomain",
        "localhost",
        "test",
    }
)

#: Storage emulators' well-known accounts (Azurite). Every install that ran the
#: demo seed has one; it identifies the emulator, not an organisation.
EMULATOR_STORAGE_ACCOUNTS: Final[frozenset[str]] = frozenset({"devstoreaccount1"})


class SignalKind(StrEnum):
    """What a fingerprint signal was derived from.

    Any scoring happens on the licence server; an install has no business
    scoring itself.
    """

    EMAIL_DOMAIN = "email_domain"
    SSO_TENANT = "sso_tenant"
    CLOUD_ACCOUNT = "cloud_account"
    STORAGE_ACCOUNT = "storage_account"
    PUBLIC_HOSTNAME = "public_hostname"
    EGRESS_NETWORK = "egress_network"


@dataclass(frozen=True, slots=True)
class FingerprintSignal:
    """One hashed organisation-level identifier.

    ``digest`` is hex HMAC-SHA256. There is deliberately no field holding the
    plaintext: once a signal is built, the original value is gone.
    """

    kind: SignalKind
    digest: str


@dataclass(frozen=True, slots=True)
class OrganizationFingerprint:
    """The complete set of signals an installation reports.

    ``excluded`` records what was deliberately dropped and why, so the admin
    settings page can show "we saw a gmail.com address and did not send it"
    rather than silently omitting it (LIC-21). The notes stay on the install:
    they can name an internal host or domain in the clear, so they are never
    part of the payload.
    """

    signals: tuple[FingerprintSignal, ...] = ()
    excluded: tuple[str, ...] = field(default=())

    def kinds(self) -> frozenset[SignalKind]:
        return frozenset(signal.kind for signal in self.signals)

    def as_payload(self) -> dict[str, list[dict[str, str]]]:
        """Exactly what goes on the wire — shown to the admin unchanged."""
        return {"signals": [{"kind": s.kind.value, "digest": s.digest} for s in self.signals]}


def _digest(salt: str, kind: SignalKind, value: str) -> str:
    """HMAC a normalised value, namespaced by its kind.

    The kind is mixed in so the same string arriving as two different signals
    cannot be cross-matched — a storage account literally named ``acme.com``
    must not collide with the email domain ``acme.com``.
    """
    message = f"{kind.value}:{value.strip().casefold()}".encode()
    return hmac.new(salt.encode(), message, sha256).hexdigest()


def email_domain(address: str) -> str | None:
    """The domain part of an email address, lowercased.

    Returns ``None`` for anything that is not a single well-formed address. The
    local part is dropped here and never returned, so no caller can accidentally
    hash a person.
    """
    address = address.strip()
    if address.count("@") != 1:
        return None
    # Internal whitespace means this is not a single address. Checked before the
    # split so "anna@ acme.com" is rejected rather than silently repaired.
    if any(char.isspace() for char in address):
        return None

    local, _, domain = address.partition("@")
    if not local:
        return None

    domain = domain.casefold().rstrip(".")
    if not domain or "." not in domain:
        return None
    return domain


def is_public_email_domain(domain: str) -> bool:
    """True for consumer providers, which are excluded from the fingerprint."""
    return domain.strip().casefold() in PUBLIC_EMAIL_DOMAINS


def is_reserved_domain(domain: str) -> bool:
    """True for example and test domains and non-delegated suffixes (``*.local``)."""
    name = domain.strip().casefold().rstrip(".")
    if name in RESERVED_DOMAINS or any(name.endswith(f".{d}") for d in RESERVED_DOMAINS):
        return True
    return any(name == s or name.endswith(f".{s}") for s in RESERVED_SUFFIXES)


def is_routable_host(host: str) -> bool:
    """True when ``host`` names something on the public internet.

    A loopback or private address, a single-label name (``azurite``, ``moto``,
    a compose service) or a reserved suffix (``*.local``) is not: it means a
    local emulator or a private name that unrelated installs share.
    """
    name = host.strip().casefold().rstrip(".").strip("[]")
    if not name:
        return False
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return "." in name and not is_reserved_domain(name)
    return address.is_global


def storage_account(config: Mapping[str, Any]) -> str | None:
    """The organisation-level storage identifier in one connector's config.

    The Azure account (``account_name``, or the first label of a host-style
    ``account_url``) or the S3/GCS bucket, prefixed with a custom endpoint's
    host because bucket names are only unique per endpoint. Never a container:
    ``media`` and ``data`` are shared by unrelated installs. ``None`` for a
    connector pointing at a non-routable endpoint — an emulator — and for
    connector types without such an identifier.
    """
    host: str | None = None
    for key in ("account_url", "endpoint_url"):
        endpoint = config.get(key)
        if isinstance(endpoint, str) and endpoint.strip():
            host = urlparse(endpoint.strip()).hostname or ""
            if not is_routable_host(host):
                return None
            break
    account = config.get("account_name") or config.get("account")
    if isinstance(account, str) and account.strip():
        return account.strip().casefold()
    if host and isinstance(config.get("account_url"), str):
        # Host-style Azure URL (path-style is only used by emulators, refused above).
        return host.split(".")[0]
    bucket = config.get("bucket")
    if isinstance(bucket, str) and bucket.strip():
        name = bucket.strip().casefold()
        return f"{host}/{name}" if host else name
    return None


def network_prefix(ip: str, *, v4_bits: int = 24, v6_bits: int = 48) -> str | None:
    """Coarsen an IP address to its network, discarding the host part.

    The full address is never fingerprinted: it identifies a machine, and on a
    home connection effectively a person. The ``/24`` (or ``/48`` for IPv6) is
    the weakest signal collected and cannot on its own raise anything
    (``docs/LICENSING.md``, *Clustering*).
    """
    try:
        address = ipaddress.ip_address(ip.strip())
    except ValueError:
        return None
    if address.is_private or address.is_loopback or address.is_link_local:
        return None
    bits = v4_bits if address.version == 4 else v6_bits
    return str(ipaddress.ip_network(f"{address}/{bits}", strict=False))


def build_fingerprint(
    salt: str,
    *,
    email_domains: list[str] | tuple[str, ...] = (),
    sso_tenant_id: str | None = None,
    cloud_account_id: str | None = None,
    storage_accounts: list[str] | tuple[str, ...] = (),
    public_hostname: str | None = None,
    egress_ip: str | None = None,
) -> OrganizationFingerprint:
    """Build the fingerprint an installation sends with its heartbeat.

    Every argument is optional: an install with telemetry partially configured
    sends fewer signals, which simply makes it harder to cluster. That is an
    accepted outcome, not a failure — an install that provides no signal is not
    presumed abusive.

    Raises ``ValueError`` on an empty salt rather than silently producing
    unsalted digests, which would be trivially reversible.
    """
    if not salt:
        raise ValueError("a fingerprint salt is required; refusing to hash unsalted")

    signals: list[FingerprintSignal] = []
    excluded: list[str] = []
    seen: set[tuple[SignalKind, str]] = set()

    def add(kind: SignalKind, value: str | None) -> None:
        if not value:
            return
        normalised = value.strip().casefold()
        if not normalised or (kind, normalised) in seen:
            return
        seen.add((kind, normalised))
        signals.append(FingerprintSignal(kind=kind, digest=_digest(salt, kind, normalised)))

    for raw in email_domains:
        domain = email_domain(raw) if "@" in raw else raw.strip().casefold()
        if not domain:
            excluded.append(f"email domain: {raw!r} is not a usable address or domain")
            continue
        if is_public_email_domain(domain):
            # Named in the clear here because it is a public provider, not a
            # customer identifier, and the admin should see why it was dropped.
            excluded.append(f"email domain: {domain} is a public provider (LIC-17)")
            continue
        if is_reserved_domain(domain):
            excluded.append(f"email domain: {domain} is reserved or private (LIC-17)")
            continue
        add(SignalKind.EMAIL_DOMAIN, domain)

    add(SignalKind.SSO_TENANT, sso_tenant_id)
    add(SignalKind.CLOUD_ACCOUNT, cloud_account_id)

    for account in storage_accounts:
        if account.strip().casefold() in EMULATOR_STORAGE_ACCOUNTS:
            excluded.append(f"storage account: {account.strip()} is an emulator account")
            continue
        add(SignalKind.STORAGE_ACCOUNT, account)

    if public_hostname:
        host = public_hostname.strip().casefold().removeprefix("https://").removeprefix("http://")
        host = host.split("/", 1)[0].split(":", 1)[0]
        if not is_routable_host(host):
            excluded.append(f"public hostname: {host} is not routable")
        else:
            add(SignalKind.PUBLIC_HOSTNAME, host)

    if egress_ip:
        prefix = network_prefix(egress_ip)
        if prefix is None:
            excluded.append("egress network: address is private, loopback or unparseable")
        else:
            add(SignalKind.EGRESS_NETWORK, prefix)

    return OrganizationFingerprint(signals=tuple(signals), excluded=tuple(excluded))
