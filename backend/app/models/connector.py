"""Storage connector configuration table."""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import JSONBType, UUIDType, pg_enum


class ConnectorType(enum.StrEnum):
    """Storage backend kind (``connector_type`` PG enum)."""

    AZURE_BLOB = "azure_blob"
    S3 = "s3"
    GCS = "gcs"
    LOCAL = "local"
    HTTP = "http"
    SHAREPOINT = "sharepoint"
    DATABRICKS_VOLUME = "databricks_volume"


class ConnectorIdentity(enum.StrEnum):
    """Credential mechanism used to reach the storage backend (``connector_identity``)."""

    MANAGED_IDENTITY = "managed_identity"
    SERVICE_PRINCIPAL = "service_principal"
    ACCOUNT_KEY = "account_key"
    SAS_TOKEN = "sas_token"
    IAM_ROLE = "iam_role"
    ACCESS_KEY = "access_key"
    NONE = "none"


class Connector(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A configured storage location (source or result) for a project."""

    __tablename__ = "connector"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[ConnectorType] = mapped_column(
        pg_enum(ConnectorType, "connector_type"), nullable=False
    )
    identity_type: Mapped[ConnectorIdentity] = mapped_column(
        pg_enum(ConnectorIdentity, "connector_identity"), nullable=False
    )
    secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict[str, object]] = mapped_column(JSONBType, nullable=False, default=dict)
    #: SHA-256 hex of the storage-event token (SRC-3); ``None`` means events are off.
    event_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
