"""DTOs for ML platforms — MLflow, Databricks, Azure ML (API-6).

Which identities and `config` fields each kind takes is checked here, so a
row that reaches the service is always usable in principle; whether the
credentials actually work is `POST /ml-platforms/{id}/check`.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field, model_validator

from app.schemas.common import BaseSchema


class MlPlatformKind(StrEnum):
    """Which MLflow host a platform is. Mirrors `app.models.MlPlatformKind`."""

    MLFLOW = "mlflow"
    DATABRICKS = "databricks"
    AZUREML = "azureml"


class MlIdentity(StrEnum):
    """How the platform authenticates to it."""

    NONE = "none"
    BEARER = "bearer"
    BASIC = "basic"
    SERVICE_PRINCIPAL = "service_principal"
    MANAGED_IDENTITY = "managed_identity"


#: Identities each kind accepts.
IDENTITIES: dict[MlPlatformKind, frozenset[MlIdentity]] = {
    MlPlatformKind.MLFLOW: frozenset({MlIdentity.NONE, MlIdentity.BEARER, MlIdentity.BASIC}),
    MlPlatformKind.DATABRICKS: frozenset({MlIdentity.BEARER, MlIdentity.SERVICE_PRINCIPAL}),
    MlPlatformKind.AZUREML: frozenset({MlIdentity.SERVICE_PRINCIPAL, MlIdentity.MANAGED_IDENTITY}),
}

#: `config` keys each kind accepts.
CONFIG_KEYS: dict[MlPlatformKind, frozenset[str]] = {
    MlPlatformKind.MLFLOW: frozenset({"ui_url"}),
    MlPlatformKind.DATABRICKS: frozenset({"client_id", "job_id"}),
    MlPlatformKind.AZUREML: frozenset({"tenant_id", "client_id"}),
}

_NEEDS_SECRET = frozenset({MlIdentity.BEARER, MlIdentity.BASIC, MlIdentity.SERVICE_PRINCIPAL})


def _text(config: dict[str, Any], key: str) -> str | None:
    value = config.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"config.{key} must be a non-empty string")
    return value


class MlPlatformCreate(BaseSchema):
    """Register a platform. `secret_ref` is a secret-store reference (AUTH-7)."""

    name: str = Field(min_length=1, max_length=255)
    kind: MlPlatformKind
    tracking_uri: str = Field(min_length=1, max_length=2000)
    identity_type: MlIdentity
    secret_ref: str | None = Field(default=None, max_length=2000)
    config: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _consistent(self) -> MlPlatformCreate:
        uri = self.tracking_uri.strip().rstrip("/")
        schemes = ("http://", "https://")
        if self.kind is MlPlatformKind.AZUREML:
            schemes = ("azureml://", "https://")
        if not uri.startswith(schemes):
            raise ValueError(f"tracking_uri must start with {' or '.join(schemes)}")
        self.tracking_uri = uri

        if self.identity_type not in IDENTITIES[self.kind]:
            allowed = ", ".join(sorted(IDENTITIES[self.kind]))
            raise ValueError(f"a {self.kind.value} platform takes identity_type {allowed}")
        if self.identity_type in _NEEDS_SECRET and not self.secret_ref:
            raise ValueError(f"identity_type {self.identity_type.value} requires a secret_ref")
        if self.identity_type not in _NEEDS_SECRET and self.secret_ref:
            raise ValueError(f"identity_type {self.identity_type.value} takes no secret_ref")

        unknown = set(self.config) - CONFIG_KEYS[self.kind]
        if unknown:
            raise ValueError(f"unknown config for {self.kind.value}: {', '.join(sorted(unknown))}")
        client_id = _text(self.config, "client_id")
        if self.identity_type is MlIdentity.SERVICE_PRINCIPAL and client_id is None:
            raise ValueError("a service principal needs config.client_id")
        if self.kind is MlPlatformKind.AZUREML:
            tenant_id = _text(self.config, "tenant_id")
            if self.identity_type is MlIdentity.SERVICE_PRINCIPAL and tenant_id is None:
                raise ValueError("a service principal needs config.tenant_id")
        ui_url = _text(self.config, "ui_url")
        if ui_url is not None:
            if not ui_url.startswith(("http://", "https://")):
                raise ValueError("config.ui_url must start with http:// or https://")
            self.config["ui_url"] = ui_url.rstrip("/")
        if "job_id" in self.config:
            job_id = self.config["job_id"]
            if isinstance(job_id, str) and job_id.isdigit():
                job_id = int(job_id)
            if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id < 1:
                raise ValueError("config.job_id must be a Databricks job id (a positive integer)")
            self.config["job_id"] = job_id
        return self


class MlPlatformUpdate(BaseSchema):
    """Partial update; the merged row is validated as a whole."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    tracking_uri: str | None = Field(default=None, min_length=1, max_length=2000)
    identity_type: MlIdentity | None = None
    secret_ref: str | None = Field(default=None, max_length=2000)
    config: dict[str, Any] | None = None


class MlPlatformRead(BaseSchema):
    """A platform as the API returns it: `secret_ref` never, only `has_secret`."""

    id: UUID
    organization_id: UUID
    name: str
    kind: MlPlatformKind
    tracking_uri: str
    identity_type: MlIdentity
    has_secret: bool
    config: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class MlPlatformCheckResult(BaseSchema):
    """`POST /ml-platforms/{id}/check`: failures are `ok: false`, never an error status."""

    ok: bool
    messages: list[str]
    info: dict[str, Any] | None = None


class SnapshotPublishRequest(BaseSchema):
    """`POST /projects/{id}/snapshots/{sid}/mlflow`."""

    ml_platform_id: UUID
    #: Default `annotation/<project name>` (`/Shared/annotation/<project name>` on Databricks).
    experiment: str | None = Field(default=None, min_length=1, max_length=500)


class SnapshotPublishResult(BaseSchema):
    """The MLflow run that stands for the snapshot."""

    ml_platform_id: UUID
    experiment_id: str
    experiment_name: str
    run_id: str
    run_url: str | None
    #: False when the snapshot already had a run in that experiment.
    created: bool


class ModelVersionImport(BaseSchema):
    """`POST /models/{id}/versions/import`: a version from an MLflow run.

    Name the run directly, or a registered model version whose run is read.
    """

    ml_platform_id: UUID
    run_id: str | None = Field(default=None, min_length=1, max_length=64)
    registered_model: str | None = Field(default=None, min_length=1, max_length=500)
    model_version: str | None = Field(default=None, min_length=1, max_length=64)
    version: int | None = Field(default=None, ge=1)
    class_mapping: dict[str, str | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_source(self) -> ModelVersionImport:
        registered = self.registered_model is not None or self.model_version is not None
        if registered and (self.registered_model is None or self.model_version is None):
            raise ValueError("give registered_model and model_version together")
        if (self.run_id is None) == (not registered):
            raise ValueError("give either run_id, or registered_model and model_version")
        return self


class MlRun(BaseSchema):
    """A run the platform started on an ML platform (retrain on Databricks)."""

    ml_platform_id: UUID
    run_id: str
    run_url: str | None
