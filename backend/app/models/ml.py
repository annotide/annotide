"""Model and model version tables (later: used once ML-3 prelabeling ships)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.db.types import JSONBType, UUIDType, pg_enum


class ModelTask(enum.StrEnum):
    """Kind of prediction task a model performs (``model_task`` PG enum)."""

    DETECT = "detect"
    SEGMENT = "segment"
    CLASSIFY = "classify"
    NER = "ner"
    LLM = "llm"
    OCR = "ocr"


class Model(UUIDPrimaryKeyMixin, Base):
    """A registered ML model endpoint, used for prelabeling (ML-3)."""

    __tablename__ = "model"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    task: Mapped[ModelTask] = mapped_column(pg_enum(ModelTask, "model_task"), nullable=False)
    #: None: an external producer (an agent) that posts its own pre-labels (API-8).
    endpoint_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    identity_type: Mapped[str] = mapped_column(String(255), nullable=False)
    secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Non-secret half of an Entra identity (BYOM-3): `scope`, `tenant_id`,
    #: `client_id`. A service principal's client secret is behind `secret_ref`.
    identity_config: Mapped[dict[str, object]] = mapped_column(
        JSONBType, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    #: Soft delete (`APP_MODEL_DELETE_MODE=soft`): the model and its versions
    #: are gone from the API, the rows stay as the author of its pre-labels.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ModelVersion(UUIDPrimaryKeyMixin, Base):
    """A specific, immutable version of a registered model."""

    __tablename__ = "model_version"

    model_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, ForeignKey("model.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    class_mapping: Mapped[dict[str, object]] = mapped_column(
        JSONBType, nullable=False, default=dict
    )
    metrics: Mapped[dict[str, object]] = mapped_column(JSONBType, nullable=False, default=dict)
    # Lineage (EXP-8): the snapshot the version was trained on, the digest it
    # proved and whatever the customer's pipeline recorded about the run.
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("snapshot.id", ondelete="SET NULL"), nullable=True, index=True
    )
    snapshot_digest: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    training_run: Mapped[dict[str, object] | None] = mapped_column(JSONBType, nullable=True)
    # Derivation (EXP-8): the version this one was trained, distilled or
    # quantized from, in any model of the organisation.
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, ForeignKey("model_version.id", ondelete="SET NULL"), nullable=True, index=True
    )
    derivation: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MlPlatformKind(enum.StrEnum):
    """Which MLflow host an `ml_platform` is (``ml_platform_kind`` PG enum, API-6)."""

    MLFLOW = "mlflow"
    DATABRICKS = "databricks"
    AZUREML = "azureml"


class MlPlatform(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A customer's MLflow server, Databricks or Azure ML workspace (API-6).

    `secret_ref` is a secret-store reference, never the credential; `config`
    holds the identity's non-secret parts (tenant / client id) and the
    Databricks retrain job id.
    """

    __tablename__ = "ml_platform"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_ml_platform_organization_name"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType,
        ForeignKey("organization.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[MlPlatformKind] = mapped_column(
        pg_enum(MlPlatformKind, "ml_platform_kind"), nullable=False
    )
    tracking_uri: Mapped[str] = mapped_column(Text, nullable=False)
    identity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    secret_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict[str, object]] = mapped_column(JSONBType, nullable=False, default=dict)
