"""Tests for the telemetry transparency and organisational-use endpoints.

The privacy promises in docs/LICENSING.md are only worth anything if they hold
at the HTTP boundary, so these tests assert on the actual response body.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.deps import CurrentUser, SettingsDep, get_current_user, get_effective_license
from app.api.v1.licensing import (
    _active_user_count,
    _collect_email_domains,
    _collect_storage_accounts,
    _licence_state,
)
from app.core.config import Settings, get_settings
from app.main import create_app
from app.services.licensing.state import EffectiveLicense, KeySource, resolve

SALT = "vendor-salt"


def admin_user() -> CurrentUser:
    return CurrentUser(
        id=uuid4(),
        organization_id=uuid4(),
        email="admin@acme.com",
        is_superuser=True,
        is_service=False,
        scopes=frozenset(),
    )


def plain_user() -> CurrentUser:
    return CurrentUser(
        id=uuid4(),
        organization_id=uuid4(),
        email="anna@acme.com",
        is_superuser=False,
        is_service=False,
        scopes=frozenset(),
    )


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://t:t@localhost/t",
        "secret_key": "test-key",
    }
    base.update(overrides)
    return Settings(**base)


async def _licence_from_settings(settings: SettingsDep) -> EffectiveLicense:
    """`get_effective_license` without a database: the env key is the only source."""
    return resolve([(KeySource.ENV, settings.license_key)], today=date.today())


@pytest.fixture
def app() -> Iterator[FastAPI]:
    application = create_app()
    application.dependency_overrides[get_current_user] = admin_user
    application.dependency_overrides[get_effective_license] = _licence_from_settings
    # No database in tests: the three collectors are the only DB touchpoints.
    application.dependency_overrides[_active_user_count] = lambda: 1
    application.dependency_overrides[_collect_email_domains] = lambda: ["acme.com"]
    application.dependency_overrides[_collect_storage_accounts] = lambda: ["acmedata"]
    application.dependency_overrides[_licence_state] = lambda: None
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


class TestTelemetryPreview:
    def test_requires_a_superuser(self, app: FastAPI, client: TestClient) -> None:
        app.dependency_overrides[get_current_user] = plain_user
        assert client.get("/api/v1/licensing/telemetry/preview").status_code == 403

    def test_without_a_salt_no_fingerprint_is_built(self, app: FastAPI, client: TestClient) -> None:
        # The salt is built in (LIC-16); only an explicit None builds no fingerprint.
        app.dependency_overrides[get_settings] = lambda: make_settings(
            license_fingerprint_salt=None
        )

        body = client.get("/api/v1/licensing/telemetry/preview").json()
        assert body["fingerprint"]["signals"] == []
        assert "salt" in (body["notice"] or "")

    def test_with_a_salt_the_payload_is_hashed(self, app: FastAPI, client: TestClient) -> None:
        app.dependency_overrides[get_settings] = lambda: make_settings(
            license_fingerprint_salt=SALT,
            telemetry_enabled=True,
            install_id="install-1",
        )

        body = client.get("/api/v1/licensing/telemetry/preview").json()
        signals = body["fingerprint"]["signals"]
        assert signals, "expected at least the email-domain signal"
        for signal in signals:
            assert set(signal) == {"kind", "digest"}
            assert len(signal["digest"]) == 64, "digest should be hex SHA-256"

    def test_no_plaintext_identifier_reaches_the_response(
        self, app: FastAPI, client: TestClient
    ) -> None:
        """The core privacy promise of LIC-16, asserted on the wire."""
        app.dependency_overrides[get_settings] = lambda: make_settings(
            license_fingerprint_salt=SALT,
            telemetry_enabled=True,
            public_hostname="annotate.acme.com",
            cloud_account_id="sub-12345",
            sso_tenant_id="tenant-guid",
        )

        raw = client.get("/api/v1/licensing/telemetry/preview").text
        for secret in ("acme.com", "acmedata", "sub-12345", "tenant-guid", "admin@"):
            assert secret not in raw, f"{secret!r} leaked into the telemetry payload"

    def test_preview_works_while_telemetry_is_disabled(
        self, app: FastAPI, client: TestClient
    ) -> None:
        # An admin must be able to inspect the payload BEFORE opting in.
        app.dependency_overrides[get_settings] = lambda: make_settings(
            license_fingerprint_salt=SALT, telemetry_enabled=False
        )

        body = client.get("/api/v1/licensing/telemetry/preview").json()
        assert body["enabled"] is False
        assert "disabled" in (body["notice"] or "")
        assert body["fingerprint"]["signals"], "the preview still shows what would be sent"

    def test_withheld_values_are_shown_but_not_in_the_payload(
        self, app: FastAPI, client: TestClient
    ) -> None:
        app.dependency_overrides[get_settings] = lambda: make_settings(
            license_fingerprint_salt=SALT, public_hostname="annotide.acme.corp"
        )
        app.dependency_overrides[_collect_email_domains] = lambda: ["gmail.com"]

        body = client.get("/api/v1/licensing/telemetry/preview").json()
        assert any("public provider" in reason for reason in body["withheld"])
        assert any("annotide.acme.corp" in reason for reason in body["withheld"])
        assert set(body["fingerprint"]) == {"signals"}
        assert "acme" not in repr(body["fingerprint"])


class TestOrganizationalUse:
    def test_requires_a_superuser(self, app: FastAPI, client: TestClient) -> None:
        app.dependency_overrides[get_current_user] = plain_user
        assert client.get("/api/v1/licensing/organizational-use").status_code == 403

    def test_company_domain_triggers_the_notice(self, app: FastAPI, client: TestClient) -> None:
        app.dependency_overrides[get_settings] = lambda: make_settings()

        body = client.get("/api/v1/licensing/organizational-use").json()
        assert body["looks_organizational"] is True
        assert body["reasons"]
        assert "Community edition" in body["message"]

    def test_a_lone_hobbyist_is_left_alone(self, app: FastAPI, client: TestClient) -> None:
        app.dependency_overrides[get_settings] = lambda: make_settings()
        app.dependency_overrides[_collect_email_domains] = lambda: ["someone@gmail.com"]
        app.dependency_overrides[_active_user_count] = lambda: 1

        body = client.get("/api/v1/licensing/organizational-use").json()
        assert body["looks_organizational"] is False
        assert body["reasons"] == []
        assert body["message"] is None

    def test_sso_and_cloud_account_are_each_a_reason(
        self, app: FastAPI, client: TestClient
    ) -> None:
        app.dependency_overrides[get_settings] = lambda: make_settings(
            sso_tenant_id="tenant", cloud_account_id="sub-1"
        )
        app.dependency_overrides[_collect_email_domains] = lambda: ["someone@gmail.com"]

        body = client.get("/api/v1/licensing/organizational-use").json()
        reasons = " ".join(body["reasons"])
        assert "single sign-on" in reasons
        assert "cloud account" in reasons

    def test_several_active_users_is_a_reason(self, app: FastAPI, client: TestClient) -> None:
        app.dependency_overrides[get_settings] = lambda: make_settings()
        app.dependency_overrides[_collect_email_domains] = lambda: ["someone@gmail.com"]
        app.dependency_overrides[_active_user_count] = lambda: 4

        body = client.get("/api/v1/licensing/organizational-use").json()
        assert any("4 users" in reason for reason in body["reasons"])

    def test_the_notice_never_threatens(self, app: FastAPI, client: TestClient) -> None:
        # LIC-19: the first contact is a sales conversation, not an accusation.
        app.dependency_overrides[get_settings] = lambda: make_settings()

        message = client.get("/api/v1/licensing/organizational-use").json()["message"]
        for word in ("violation", "illegal", "breach", "suspend", "terminate", "unauthorised"):
            assert word not in message.lower()
