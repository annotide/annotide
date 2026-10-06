"""Tests for the organisation fingerprint the heartbeat sends (LIC-6, LIC-9).

Clustering runs on the vendor's licence server and is tested there.
"""

from __future__ import annotations

import pytest

from app.services.licensing.fingerprint import (
    PUBLIC_EMAIL_DOMAINS,
    SignalKind,
    build_fingerprint,
    email_domain,
    is_public_email_domain,
    is_reserved_domain,
    is_routable_host,
    network_prefix,
    storage_account,
)

SALT = "vendor-salt-for-tests"


# --------------------------------------------------------------------------- #
# Fingerprint
# --------------------------------------------------------------------------- #


class TestEmailDomain:
    @pytest.mark.parametrize(
        ("address", "expected"),
        [
            ("anna@acme.com", "acme.com"),
            ("Anna.Virtanen@ACME.COM", "acme.com"),
            ("  anna@acme.com  ", "acme.com"),
            ("anna@acme.com.", "acme.com"),
            ("anna@sub.acme.co.uk", "sub.acme.co.uk"),
        ],
    )
    def test_extracts_the_domain(self, address: str, expected: str) -> None:
        assert email_domain(address) == expected

    @pytest.mark.parametrize(
        "address",
        ["", "not-an-email", "a@b@c.com", "anna@", "@acme.com", "anna@localhost", "anna@ acme.com"],
    )
    def test_rejects_malformed(self, address: str) -> None:
        assert email_domain(address) is None

    def test_local_part_never_survives(self) -> None:
        # The whole privacy argument rests on this.
        assert "anna" not in (email_domain("anna@acme.com") or "")


class TestPublicDomains:
    def test_common_providers_are_public(self) -> None:
        for domain in ("gmail.com", "outlook.com", "proton.me", "yahoo.com"):
            assert is_public_email_domain(domain)

    def test_company_domain_is_not_public(self) -> None:
        assert not is_public_email_domain("acme.com")

    def test_case_insensitive(self) -> None:
        assert is_public_email_domain("GMAIL.COM")

    def test_public_domains_are_excluded_not_hashed(self) -> None:
        fp = build_fingerprint(SALT, email_domains=["someone@gmail.com"])
        assert fp.signals == ()
        assert any("public provider" in reason for reason in fp.excluded)

    def test_every_listed_domain_is_lowercase(self) -> None:
        # Lookup is casefolded, so an uppercase entry would silently never match.
        assert all(d == d.casefold() for d in PUBLIC_EMAIL_DOMAINS)


class TestNetworkPrefix:
    def test_ipv4_is_coarsened_to_24(self) -> None:
        assert network_prefix("8.8.8.8") == "8.8.8.0/24"

    def test_ipv6_is_coarsened_to_48(self) -> None:
        assert network_prefix("2a00:1450:4001:80f::200e") == "2a00:1450:4001::/48"

    def test_host_part_is_discarded(self) -> None:
        assert network_prefix("8.8.8.1") == network_prefix("8.8.8.254")

    @pytest.mark.parametrize(
        "ip", ["10.0.0.5", "192.168.1.1", "172.16.0.1", "127.0.0.1", "169.254.1.1", "nonsense", ""]
    )
    def test_private_and_invalid_are_dropped(self, ip: str) -> None:
        assert network_prefix(ip) is None

    @pytest.mark.parametrize("ip", ["203.0.113.47", "198.51.100.1", "2001:db8:1234:5678::1"])
    def test_documentation_ranges_are_dropped(self, ip: str) -> None:
        # TEST-NET and 2001:db8:: are never a real egress address, so treating
        # them as a signal would cluster unrelated test installs together.
        assert network_prefix(ip) is None


class TestBuildFingerprint:
    def test_empty_salt_is_refused(self) -> None:
        with pytest.raises(ValueError, match="salt"):
            build_fingerprint("", email_domains=["anna@acme.com"])

    def test_same_domain_hashes_identically_across_installs(self) -> None:
        # This is the property the whole design depends on.
        a = build_fingerprint(SALT, email_domains=["anna@acme.com"])
        b = build_fingerprint(SALT, email_domains=["bob@acme.com"])
        assert a.signals[0].digest == b.signals[0].digest

    def test_different_salt_gives_a_different_digest(self) -> None:
        a = build_fingerprint(SALT, email_domains=["anna@acme.com"])
        b = build_fingerprint("other-salt", email_domains=["anna@acme.com"])
        assert a.signals[0].digest != b.signals[0].digest

    def test_kinds_are_namespaced_against_each_other(self) -> None:
        # A storage account named "acme.com" must not match the email domain.
        fp = build_fingerprint(SALT, email_domains=["acme.com"], storage_accounts=["acme.com"])
        digests = {s.digest for s in fp.signals}
        assert len(digests) == 2

    def test_no_plaintext_appears_in_the_payload(self) -> None:
        fp = build_fingerprint(
            SALT,
            email_domains=["anna@acme.com"],
            sso_tenant_id="a1b2c3d4-0000-0000-0000-000000000000",
            cloud_account_id="123456789012",
            storage_accounts=["acmedata"],
            public_hostname="annotate.acme.com",
        )
        blob = repr(fp.as_payload())
        for secret in ("anna", "acme.com", "a1b2c3d4", "123456789012", "acmedata"):
            assert secret not in blob

    def test_all_signal_kinds_can_be_produced(self) -> None:
        fp = build_fingerprint(
            SALT,
            email_domains=["anna@acme.com"],
            sso_tenant_id="tenant-guid",
            cloud_account_id="sub-123",
            storage_accounts=["acmedata"],
            public_hostname="https://annotate.acme.com:443/app",
            egress_ip="8.8.8.8",
        )
        assert fp.kinds() == set(SignalKind)

    def test_hostname_is_normalised(self) -> None:
        plain = build_fingerprint(SALT, public_hostname="annotate.acme.com")
        messy = build_fingerprint(SALT, public_hostname="HTTPS://Annotate.Acme.com:8443/path")
        assert plain.signals[0].digest == messy.signals[0].digest

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "myserver.local"])
    def test_non_routable_hostnames_are_excluded(self, host: str) -> None:
        fp = build_fingerprint(SALT, public_hostname=host)
        assert fp.signals == ()
        assert fp.excluded

    def test_duplicates_are_collapsed(self) -> None:
        fp = build_fingerprint(SALT, email_domains=["a@acme.com", "b@acme.com", "c@ACME.com"])
        assert len(fp.signals) == 1

    def test_nothing_configured_yields_an_empty_fingerprint(self) -> None:
        fp = build_fingerprint(SALT)
        assert fp.signals == ()
        assert fp.kinds() == frozenset()

    def test_excluded_reasons_are_reported_for_the_admin(self) -> None:
        fp = build_fingerprint(SALT, email_domains=["x@gmail.com"], egress_ip="10.0.0.1")
        assert len(fp.excluded) == 2

    def test_excluded_reasons_stay_on_the_install(self) -> None:
        fp = build_fingerprint(
            SALT, email_domains=["anna@acme.internal"], public_hostname="annotide.acme.corp"
        )
        assert len(fp.excluded) == 2
        assert fp.as_payload() == {"signals": []}
        assert "acme" not in repr(fp.as_payload())


class TestSharedDefaultsAreExcluded:
    """Values every trial install shares must not cluster unrelated installs."""

    @pytest.mark.parametrize(
        "domain",
        [
            "example.com",
            "mail.example.org",
            "acme.test",
            "corp.local",
            "ad.internal",
            "x.localhost",
            # Service accounts' synthetic addresses (api_keys.create_service_account).
            "service.invalid",
        ],
    )
    def test_reserved_email_domains_are_excluded(self, domain: str) -> None:
        assert is_reserved_domain(domain)
        fp = build_fingerprint(SALT, email_domains=[f"demo@{domain}"])
        assert fp.signals == ()
        assert "reserved" in fp.excluded[0]

    @pytest.mark.parametrize("domain", ["acme.com", "example-corp.com", "localhost-labs.io"])
    def test_real_domains_are_not_reserved(self, domain: str) -> None:
        assert not is_reserved_domain(domain)

    @pytest.mark.parametrize(
        ("host", "routable"),
        [
            ("annotate.acme.com", True),
            ("8.8.8.8", True),
            ("azurite", False),
            ("moto", False),
            ("10.1.2.3", False),
            ("[::1]", False),
            ("files.example.com", False),
            ("", False),
        ],
    )
    def test_is_routable_host(self, host: str, routable: bool) -> None:
        assert is_routable_host(host) is routable

    @pytest.mark.parametrize(
        ("config", "expected"),
        [
            (
                {"account_url": "https://acmedata.blob.core.windows.net", "container": "media"},
                "acmedata",
            ),
            (
                {"account_url": "https://x.blob.core.windows.net", "account_name": "AcmeData"},
                "acmedata",
            ),
            ({"bucket": "acme-media", "region": "eu-west-1"}, "acme-media"),
            ({"bucket": "media", "endpoint_url": "https://s3.acme.com"}, "s3.acme.com/media"),
            # Emulators: Azurite, Moto and fake-gcs-server from the demo seeds.
            ({"account_url": "http://azurite:10000/devstoreaccount1", "container": "media"}, None),
            ({"account_url": "http://localhost:10000/devstoreaccount1"}, None),
            ({"bucket": "media", "endpoint_url": "http://moto:5000"}, None),
            ({"bucket": "media", "endpoint_url": "http://127.0.0.1:4443"}, None),
            # A container alone is never an organisation identifier.
            ({"container": "media"}, None),
            ({"root": "/data"}, None),
        ],
    )
    def test_storage_account(self, config: dict[str, str], expected: str | None) -> None:
        assert storage_account(config) == expected

    def test_emulator_account_is_excluded_from_the_fingerprint(self) -> None:
        fp = build_fingerprint(SALT, storage_accounts=["devstoreaccount1", "acmedata"])
        assert [s.kind for s in fp.signals] == [SignalKind.STORAGE_ACCOUNT]
        assert "emulator" in fp.excluded[0]
