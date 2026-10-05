"""Request/response DTOs for the connector entity."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field

from app.schemas.common import BaseSchema


class ConnectorType(StrEnum):
    """Storage backend a connector talks to."""

    AZURE_BLOB = "azure_blob"
    S3 = "s3"
    GCS = "gcs"
    LOCAL = "local"
    HTTP = "http"
    SHAREPOINT = "sharepoint"
    DATABRICKS_VOLUME = "databricks_volume"


class ConnectorIdentity(StrEnum):
    """How a connector authenticates to its backend."""

    MANAGED_IDENTITY = "managed_identity"
    SERVICE_PRINCIPAL = "service_principal"
    ACCOUNT_KEY = "account_key"
    SAS_TOKEN = "sas_token"
    IAM_ROLE = "iam_role"
    ACCESS_KEY = "access_key"
    NONE = "none"


class ConnectorCreate(BaseSchema):
    """Payload to register a new storage connector.

    `secret_ref` is a Key Vault / secret-store reference, never a raw secret
    (SEC/AUTH-7).
    """

    name: str
    type: ConnectorType
    identity_type: ConnectorIdentity
    secret_ref: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class ConnectorUpdate(BaseSchema):
    """Partial update payload for a connector; all fields optional."""

    name: str | None = None
    type: ConnectorType | None = None
    identity_type: ConnectorIdentity | None = None
    secret_ref: str | None = None
    config: dict[str, Any] | None = None


class ConnectorRead(BaseSchema):
    """Connector as returned by the API; `secret_ref` is never exposed, only its presence."""

    id: UUID
    organization_id: UUID
    name: str
    type: ConnectorType
    identity_type: ConnectorIdentity
    has_secret: bool
    #: Whether a storage-event token is set (SRC-3); the token itself is never returned.
    events_enabled: bool = False
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime
