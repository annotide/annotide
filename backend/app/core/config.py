"""Application configuration loaded from ``APP_``-prefixed environment variables."""

from __future__ import annotations

import ipaddress
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

#: The vendor-wide fingerprint salt (LIC-16). Public by design: every install
#: needs the same value, so it cannot be kept secret, and the telemetry page
#: says so. Changing it splits clusters across versions; don't.
FINGERPRINT_SALT = "58e8f0f99d8f3dc87c85d09314dbab8411c52654bb26783375733142a9453518"


class BackingServiceAuth(StrEnum):
    """How the backend signs in to its own PostgreSQL and Redis (SEC-1)."""

    #: The credential is in `APP_DATABASE_URL` / `APP_REDIS_URL`.
    PASSWORD = "password"
    #: A Microsoft Entra token for the host's managed identity, refreshed
    #: before it expires (`app.core.entra`). The URL carries no password.
    ENTRA = "entra"


_MIN_PRODUCTION_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    """Runtime settings for the application, see ``docs/CONTRACTS.md`` (OPS-1)."""

    model_config = SettingsConfigDict(env_prefix="APP_", case_sensitive=False, extra="ignore")

    env: str = "development"
    database_url: str
    redis_url: str = "redis://localhost:6379/0"
    database_auth: BackingServiceAuth = BackingServiceAuth.PASSWORD
    redis_auth: BackingServiceAuth = BackingServiceAuth.PASSWORD
    #: Client id of the user-assigned managed identity to sign in as, for
    #: `entra` auth and Key Vault. Unset: the system-assigned identity, or on
    #: AKS the workload identity named by `AZURE_CLIENT_ID`.
    managed_identity_client_id: str | None = None
    secret_key: str | None = None
    #: The key `APP_SECRET_KEY` replaced, during a rotation. Everything new is
    #: signed and sealed with the current key; tokens, media URLs and sealed
    #: secrets made with this one are still accepted. Run
    #: `python -m app.cli reseal-secrets`, wait out `APP_ACCESS_TOKEN_TTL`,
    #: then remove it. API keys (`ant_…`) never depend on either.
    secret_key_previous: str | None = None
    access_token_ttl: int = 3600
    #: AUTH-2: a superuser signing in with a password must have MFA. Without
    #: it they get a token good only for `/auth/mfa` and `GET /auth/me` until they
    #: turn it on. SSO sign-ins are left to the identity provider's policy.
    mfa_required_for_admins: bool = False
    signed_url_ttl: int = 900
    #: How a model service reaches this API, e.g. ``http://backend:8000``.
    #: Connectors that proxy media through the API (`local`,
    #: `databricks_volume`) put it in front of `internal` signed URLs; unset,
    #: those URLs stay root-relative and only a browser can fetch them.
    internal_api_url: str | None = None
    task_lock_ttl: int = 1800
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]
    #: Reverse proxies whose `X-Forwarded-For` / `X-Forwarded-Proto` are
    #: believed, as IPs or CIDRs. Empty (the default) trusts nobody: the audit
    #: log then records the direct peer and forwarded headers are ignored (SEC-3).
    trusted_proxies: Annotated[list[str], NoDecode] = []
    log_level: str = "INFO"
    log_format: str = "json"
    #: OpenTelemetry traces and metrics (OPS-3) to the operator's own OTLP
    #: collector, configured with the standard `OTEL_*` variables. Needs the
    #: backend's `otel` extra. Not vendor telemetry: nothing goes to us.
    otel_enabled: bool = False

    # --- Background worker (ARC-4, DATA-2) ---------------------------------
    #: How many unpublished outbox rows the publisher claims per transaction.
    outbox_poll_batch_size: int = 50
    #: An outbox row that failed this many times is left for an operator
    #: instead of being retried every minute forever.
    outbox_max_attempts: int = 10
    #: Total attempts a background job gets before it is marked failed.
    job_max_tries: int = 5
    #: Jobs of one project running at once (scan, export, pre-label, …). More
    #: wait in `queued` and start as slots free up, so a few huge jobs of one
    #: project cannot occupy every worker slot. Waiting costs no attempts.
    job_max_running_per_project: int = Field(default=3, ge=1)
    #: `DELETE /models/{id}`: `soft` hides the model and keeps its versions, so
    #: pre-labels it wrote keep their author; `hard` removes it, and is refused
    #: (409) while any annotation was written by one of its versions.
    model_delete_mode: Literal["soft", "hard"] = "soft"
    #: Longest side of generated thumbnails, px (IMG-8).
    thumbnail_size: int = 256
    #: Sources larger than this are skipped by the thumbnail job (64 MiB).
    thumbnail_max_source_bytes: int = 64 * 1024 * 1024
    #: Images with at least this many pixels (width x height) are tiled (IMG-1).
    tile_min_pixels: int = 25_000_000
    #: Sources larger than this are not tiled (12 GiB, IMG-2).
    tile_max_source_bytes: int = 12 * 1024**3
    #: Where the worker stages a source and its pyramid while tiling; system
    #: temp dir when unset.
    tile_work_dir: str | None = None

    # --- Webhooks (API-4) --------------------------------------------------
    #: Attempts per delivery before it is marked failed (exponential backoff
    #: from 30 s; 8 attempts span roughly two hours).
    webhook_max_attempts: int = 8
    #: Seconds to wait for a subscriber to answer one delivery.
    webhook_timeout: float = 10.0
    # API-5: fixed one-minute windows in Redis; fail open when Redis is down.
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = Field(default=1200, ge=1)
    rate_limit_api_key_per_minute: int = Field(default=1200, ge=1)
    rate_limit_login_per_minute: int = Field(default=10, ge=1)
    #: How many due deliveries one worker tick sends.
    webhook_poll_batch_size: int = 50
    #: Deliveries one webhook receives per minute at most (API-4). A burst
    #: (a bulk approve of 500 items is 1000 events) is spread over the next
    #: minutes instead of tripping the receiver's own limit — Slack takes about
    #: one message a second. Nothing is dropped.
    webhook_max_per_minute: int = Field(default=60, ge=1)
    #: Let webhooks reach private, loopback and link-local addresses (SEC-4).
    #: Off: a hook cannot be pointed at internal services or cloud metadata.
    webhook_allow_private_urls: bool = False

    # --- Notification e-mail (API-7) ---------------------------------------
    #: SMTP server; unset, nothing is ever mailed.
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    #: `starttls` (upgrade a plain connection), `ssl` (implicit TLS) or `none`.
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from: str = "Annotide <noreply@localhost>"
    #: Seconds; an unsent notification older than this is never mailed, so
    #: turning e-mail on does not mail the whole backlog.
    notification_email_max_age: int = Field(default=86400, ge=60)

    # --- Licensing and telemetry (LIC-6, LIC-9, LIC-16, LIC-21, LIC-27) ----
    #: Vendor licence server base URL for the licence refresh and the
    #: heartbeat. Unset, neither ever sends anything.
    license_server_url: str | None = None
    #: Daily licence refresh for keyed installs (LIC-27). On by default: it is
    #: licence accounting, not telemetry, and how a paid key renews itself.
    license_refresh_enabled: bool = True
    #: On by default, opt-out: ``false`` stops the heartbeat entirely. An
    #: installation that turns it off is not presumed abusive, it simply
    #: provides no clustering signal. Nothing is sent without a licence server.
    telemetry_enabled: bool = True
    #: Stable identifier for this installation, sent with the heartbeat.
    install_id: str | None = None
    #: Vendor-wide HMAC salt for the organisation fingerprint. Shared across
    #: installs on purpose: two installs must hash the same domain identically
    #: or clustering cannot work. That makes it public, not a secret, so it
    #: ships in the code (LIC-16); empty means the built-in value. To send no
    #: fingerprint, turn the heartbeat off (``telemetry_enabled``).
    license_fingerprint_salt: str | None = FINGERPRINT_SALT
    #: Licence key (LIC-1). Unset = Community mode. Verified offline against the
    #: vendor public keys compiled into ``services/licensing/keys.py``.
    license_key: str | None = None
    #: This installation's public hostname, if it has one. One fingerprint
    #: signal, and nothing else depends on it.
    public_hostname: str | None = None
    #: Cloud account / subscription / project id, when the deployment knows it.
    cloud_account_id: str | None = None
    #: SSO tenant id, when an OIDC provider is configured.
    sso_tenant_id: str | None = None

    # --- OIDC single sign-on (AUTH-1) -------------------------------------
    #: Issuer URL of the identity provider. Setting it turns SSO on; discovery
    #: is read from ``{issuer}/.well-known/openid-configuration``.
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    #: Optional: public clients authenticate with PKCE alone.
    oidc_client_secret: str | None = None
    #: The callback the provider redirects to, registered with the provider:
    #: ``https://<api-host>/api/v1/auth/oidc/callback``.
    oidc_redirect_uri: str | None = None
    oidc_scopes: str = "openid profile email"
    #: Label on the sign-in button.
    oidc_display_name: str = "Single sign-on"
    #: Organisation a first-time SSO user is provisioned into. Unset means
    #: the only organisation in the database; with several, provisioning is
    #: refused until this is set.
    oidc_organization_slug: str | None = None
    #: Create an account on first SSO sign-in. Off means only users who
    #: already exist (by ``idp_subject`` or verified e-mail) can sign in.
    oidc_auto_provision: bool = True
    #: Link a first SSO sign-in to an existing account by e-mail even when the
    #: ID token does not carry ``email_verified: true``. Only for providers
    #: where users cannot set their own address (Entra ID sends no such claim).
    oidc_trust_unverified_email: bool = False
    #: ID token claim carrying the user's groups for AUTH-3 sync; empty
    #: turns group sync off.
    oidc_groups_claim: str = "groups"
    #: Comma-separated IdP groups whose members are superusers. Unset leaves
    #: ``is_superuser`` to the application (AUTH-3).
    oidc_admin_groups: str | None = None
    #: Where the browser is sent after SSO completes (and on SSO errors).
    frontend_url: str = "http://localhost:5173"

    # --- Secret stores (AUTH-7) -------------------------------------------
    #: How long a resolved secret stays cached in-process. Keeps a vault
    #: from being called once per request.
    secret_cache_ttl: int = 300
    #: Restrict `file:` references to this directory. Unset means any
    #: readable path, which is fine for dev and unwise in production.
    secret_file_root: str | None = None
    #: Vault used when an `azurekeyvault://` reference names only a secret.
    azure_key_vault_name: str | None = None
    #: Region used when an `awssecrets://` reference names none.
    aws_region: str | None = None

    @field_validator(
        "install_id",
        "license_key",
        "license_server_url",
        "public_hostname",
        "cloud_account_id",
        "sso_tenant_id",
        # `tempfile` reads dir="" as the working directory, not the temp dir.
        "tile_work_dir",
        "oidc_issuer",
        "oidc_client_id",
        "oidc_client_secret",
        "oidc_redirect_uri",
        "oidc_organization_slug",
        "oidc_admin_groups",
        "secret_file_root",
        "azure_key_vault_name",
        "managed_identity_client_id",
        "aws_region",
        mode="before",
    )
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        """`APP_X=` (as docker compose passes `${APP_X:-}`) means unset, not ""."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("license_fingerprint_salt", mode="before")
    @classmethod
    def _empty_salt_is_built_in(cls, value: object) -> object:
        """`APP_LICENSE_FINGERPRINT_SALT=` keeps the vendor-wide salt (LIC-16)."""
        if isinstance(value, str) and not value.strip():
            return FINGERPRINT_SALT
        return value

    @field_validator("cors_origins", mode="after")
    @classmethod
    def _refuse_wildcard_cors_origin(cls, value: list[str]) -> list[str]:
        """`*` with credentialed requests would let any site call the API as the user."""
        if any(origin.strip() == "*" for origin in value):
            raise ValueError("APP_CORS_ORIGINS must list explicit origins; '*' is not allowed")
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: object) -> list[str]:
        """Parse ``APP_CORS_ORIGINS`` as a comma-separated string into a list."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        if isinstance(value, list):
            return value
        raise TypeError("APP_CORS_ORIGINS must be a comma-separated string or a list")

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _split_trusted_proxies(cls, value: object) -> list[str]:
        """Parse ``APP_TRUSTED_PROXIES`` as a comma-separated string into a list."""
        if isinstance(value, str):
            return [entry.strip() for entry in value.split(",") if entry.strip()]
        if isinstance(value, list):
            return value
        raise TypeError("APP_TRUSTED_PROXIES must be a comma-separated string or a list")

    @field_validator("trusted_proxies", mode="after")
    @classmethod
    def _validate_trusted_proxies(cls, value: list[str]) -> list[str]:
        """Refuse entries that are not IP addresses or networks, at startup."""
        for entry in value:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError as exc:
                raise ValueError(f"APP_TRUSTED_PROXIES: {entry!r} is not an IP or CIDR") from exc
        return value

    @model_validator(mode="after")
    def _require_secret_key_in_production(self) -> Self:
        """Enforce a real JWT signing key for production deployments."""
        if self.env != "production":
            return self
        if not self.secret_key:
            raise ValueError("APP_SECRET_KEY is required when APP_ENV=production")
        if len(self.secret_key) < _MIN_PRODUCTION_SECRET_KEY_LENGTH or self.secret_key.startswith(
            "dev-only"
        ):
            raise ValueError(
                "APP_SECRET_KEY must be at least 32 characters and not the dev placeholder "
                "when APP_ENV=production"
            )
        return self

    @model_validator(mode="after")
    def _require_complete_oidc_config(self) -> Self:
        """An issuer without a client id or redirect URI cannot complete a login."""
        if self.oidc_issuer and not (self.oidc_client_id and self.oidc_redirect_uri):
            raise ValueError(
                "APP_OIDC_CLIENT_ID and APP_OIDC_REDIRECT_URI are required with APP_OIDC_ISSUER"
            )
        return self

    @property
    def oidc_enabled(self) -> bool:
        """True when an identity provider is configured (AUTH-1)."""
        return bool(self.oidc_issuer)


@lru_cache
def get_settings() -> Settings:
    """Return a process-wide cached ``Settings`` instance built from the environment."""
    return Settings()
