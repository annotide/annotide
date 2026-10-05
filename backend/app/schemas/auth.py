"""Request/response DTOs for authentication and the user entity."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.schemas.common import BaseSchema


class LoginRequest(BaseSchema):
    """Local login credentials (AUTH-2)."""

    email: str
    password: str
    #: A TOTP code or a recovery code, for accounts with MFA on (AUTH-2).
    otp: str | None = Field(default=None, max_length=32)


class OidcProviderInfo(BaseSchema):
    """The configured single sign-on provider, as the login page sees it (AUTH-1)."""

    display_name: str
    #: Path under the API base the browser navigates to (a full-page
    #: redirect, not an XHR); accepts ``?next=<path>``.
    login_path: str = "/auth/oidc/login"


class AuthProviders(BaseSchema):
    """Which sign-in methods this installation offers."""

    local: bool = True
    oidc: OidcProviderInfo | None = None


class TokenResponse(BaseSchema):
    """Access token issued on successful login."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int
    #: `APP_MFA_REQUIRED_FOR_ADMINS`: the token only reaches `/auth/mfa` and
    #: `GET /auth/me` until MFA is on; sign in again afterwards.
    mfa_setup_required: bool = False


class UserCreate(BaseSchema):
    """Payload to create a new local user.

    `password`, if given, is hashed server-side (argon2, AUTH-2) and never
    stored or returned as-is.
    """

    organization_id: UUID
    email: str
    display_name: str
    password: str | None = None
    idp_subject: str | None = None


class UserUpdate(BaseSchema):
    """Partial update payload for a user; all fields optional."""

    display_name: str | None = None
    password: str | None = None
    idp_subject: str | None = None
    is_active: bool | None = None


class UserRead(BaseSchema):
    """User as returned by the API. `password_hash` is never exposed."""

    id: UUID
    organization_id: UUID
    email: str
    display_name: str
    idp_subject: str | None
    is_active: bool
    is_superuser: bool
    is_service: bool = False
    last_seen_at: datetime | None
    #: TOTP MFA is on (AUTH-2).
    mfa_enabled: bool = False
    #: Set once the person was pseudonymised (SEC-6).
    erased_at: datetime | None = None
    #: E-mail copies of in-app notifications (API-7).
    email_notifications: bool = True
    created_at: datetime
    updated_at: datetime


class UserPreferencesUpdate(BaseSchema):
    """`PATCH /auth/me`: the caller's own preferences; only keys present change."""

    email_notifications: bool | None = None


class ServiceAccountCreate(BaseSchema):
    """Payload to create a service account (AUTH-4); the e-mail is synthetic."""

    display_name: str = Field(min_length=1, max_length=255)


class ApiKeyCreate(BaseSchema):
    """Payload to mint an API key (AUTH-4).

    `scopes` is a subset of `read` / `write` / `admin`; `user_id` (superuser
    only) issues the key for another user or a service account.
    """

    name: str = Field(min_length=1, max_length=255)
    scopes: list[str] = Field(default_factory=lambda: ["read"], min_length=1)
    expires_at: datetime | None = None
    user_id: UUID | None = None


class ApiKeyRead(BaseSchema):
    """API key metadata; the token itself is only returned at creation."""

    id: UUID
    organization_id: UUID
    user_id: UUID
    name: str
    scopes: list[str]
    expires_at: datetime | None
    last_used_at: datetime | None
    revoked_at: datetime | None
    created_by: UUID | None
    created_at: datetime


class ApiKeyCreated(ApiKeyRead):
    """The freshly minted key, carrying the bearer `token` exactly once."""

    token: str


class MfaStatus(BaseSchema):
    """`GET /auth/mfa`: the caller's MFA state (AUTH-2)."""

    #: MFA is on: sign-in asks for a code.
    enabled: bool
    #: A seed from `/auth/mfa/setup` is waiting to be confirmed.
    pending: bool
    recovery_codes_left: int
    #: False for SSO-only and service accounts, which cannot use it here.
    available: bool


class MfaSetup(BaseSchema):
    """`POST /auth/mfa/setup`: the seed to add to an authenticator app."""

    secret: str
    otpauth_uri: str


class MfaCode(BaseSchema):
    """A six-digit code from the authenticator app, or a recovery code where allowed."""

    code: str = Field(min_length=1, max_length=32)


class MfaRecoveryCodes(BaseSchema):
    """Single-use recovery codes, shown once."""

    recovery_codes: list[str]
