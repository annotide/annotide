"""Organization, user, project-membership, API-key and SCIM-group tables."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import JSONBType, UUIDType, pg_enum


class ProjectRole(enum.StrEnum):
    """A user's role on a single project (``project_role`` PG enum)."""

    OWNER = "owner"
    ANNOTATOR = "annotator"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


class MembershipSource(enum.StrEnum):
    """Who granted a membership (``membership_source`` PG enum, AUTH-3)."""

    MANUAL = "manual"
    IDP = "idp"


class Organization(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A tenant of the platform."""

    __tablename__ = "organization"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    #: SHA-256 hex of the SCIM bearer token (AUTH-3); SCIM is off while null.
    scim_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A person who can sign in, either via local password or an OIDC IdP."""

    __tablename__ = "user"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(CITEXT(), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    idp_subject: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    is_superuser: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # A service account (AUTH-4): cannot sign in, acts only through API keys.
    is_service: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # MFA (AUTH-2, `services/mfa.py`). The seed is sealed with a key derived
    # from APP_SECRET_KEY; MFA is on once `totp_enabled_at` is set.
    totp_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    totp_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The last accepted time step: a code works once.
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: SHA-256 hex of the unused recovery codes.
    mfa_recovery_codes: Mapped[list[str] | None] = mapped_column(JSONBType, nullable=True)
    #: Set by a GDPR erasure (SEC-6); the row is pseudonymised, not deleted.
    erased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The IdP's SCIM `externalId` (AUTH-3).
    scim_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: A SCIM `DELETE` deactivated the account and hid it from SCIM.
    scim_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: E-mail copies of in-app notifications (API-7); the user's own switch.
    email_notifications: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    @property
    def mfa_enabled(self) -> bool:
        return self.totp_enabled_at is not None


class Membership(UUIDPrimaryKeyMixin, Base):
    """A user's role assignment on a project."""

    __tablename__ = "membership"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("project.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[ProjectRole] = mapped_column(pg_enum(ProjectRole, "project_role"), nullable=False)
    #: `idp` memberships are owned by SSO group sync; `manual` ones never are.
    source: Mapped[MembershipSource] = mapped_column(
        pg_enum(MembershipSource, "membership_source"),
        nullable=False,
        default=MembershipSource.MANUAL,
        server_default=MembershipSource.MANUAL.value,
    )
    #: Folder-level access: only items under these path prefixes; None = all.
    path_prefixes: Mapped[list[str] | None] = mapped_column(JSONBType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ApiKeyScope(enum.StrEnum):
    """What an API key may do (AUTH-4). Each level implies the previous one."""

    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


class ApiKey(UUIDPrimaryKeyMixin, Base):
    """Metadata for an API key (AUTH-4).

    The key is an opaque ``ant_<token_prefix>_<secret>`` token: the row keeps
    the prefix (to find it) and a sha256 of the whole token (to check it), so
    the key neither depends on ``APP_SECRET_KEY`` nor can be read back from
    the database. Keys issued before migration 0032 are JWTs carrying this
    row's id as ``kid`` and have neither column. Resolving the row on every
    request is what makes revocation and expiry immediate.
    """

    __tablename__ = "api_key"
    __table_args__ = (UniqueConstraint("token_prefix", name="uq_api_key_token_prefix"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONBType, nullable=False, default=list)
    token_prefix: Mapped[str | None] = mapped_column(String(16), nullable=True)
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ScimGroup(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A group an IdP pushed over SCIM (AUTH-3); feeds IdP group sync."""

    __tablename__ = "scim_group"
    __table_args__ = (
        UniqueConstraint("organization_id", "display_name", name="uq_scim_group_name"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)


class ScimGroupMember(UUIDPrimaryKeyMixin, Base):
    """A user's membership of a SCIM group."""

    __tablename__ = "scim_group_member"
    __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_scim_group_member"),)

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("scim_group.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
