"""Request/response DTOs for the model / model_version entities (ML-1, BYOM-2, BYOM-3)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.schemas.common import BaseSchema


class ModelTask(StrEnum):
    """Kind of prediction task a registered model performs."""

    DETECT = "detect"
    SEGMENT = "segment"
    CLASSIFY = "classify"
    NER = "ner"
    LLM = "llm"
    OCR = "ocr"


class ModelIdentity(StrEnum):
    """How the platform authenticates to a model endpoint (BYOM-3).

    Kept in sync with `app.services.models.ModelIdentity` by value; the
    schema layer may not import `services/`, so the enum is duplicated here.
    """

    NONE = "none"
    API_KEY = "api_key"
    BEARER = "bearer"
    #: Microsoft Entra: an app registration's client secret (in `secret_ref`).
    SERVICE_PRINCIPAL = "service_principal"
    #: Microsoft Entra: the platform's own managed identity; no secret.
    MANAGED_IDENTITY = "managed_identity"


class ModelIdentityConfig(BaseSchema):
    """The non-secret half of an Entra identity (BYOM-3).

    `scope` is the token audience, e.g. `https://ml.azure.com/.default` for an
    Azure ML online endpoint or `https://cognitiveservices.azure.com/.default`
    for Azure OpenAI / AI Foundry. `tenant_id` and `client_id` name a service
    principal; for a managed identity, `client_id` picks a user-assigned one
    (omitted: the system-assigned or workload identity).
    """

    scope: str | None = Field(default=None, max_length=512)
    tenant_id: str | None = Field(default=None, max_length=255)
    client_id: str | None = Field(default=None, max_length=255)

    @field_validator("scope")
    @classmethod
    def _validate_scope(cls, value: str | None) -> str | None:
        # Managed identities and service principals sign in with the client
        # credentials flow, which only issues `<resource>/.default` tokens.
        if value is not None and not value.endswith("/.default"):
            raise ValueError("scope must end with /.default, e.g. https://ml.azure.com/.default")
        return value


def _validate_endpoint_url(value: str) -> str:
    if not value.startswith(("http://", "https://")):
        raise ValueError("endpoint_url must start with http:// or https://")
    return value


class ModelCreate(BaseSchema):
    """Payload to register a new model endpoint.

    `secret_ref` is a secret-store reference, never a raw credential
    (SEC/AUTH-7): the key or token for `api_key` / `bearer`, the client
    secret for `service_principal`, and absent for `none` and
    `managed_identity`. Entra identities also need `identity_config`.
    """

    name: str = Field(min_length=1, max_length=255)
    task: ModelTask
    #: Omitted: an external producer, e.g. an AI agent posting pre-labels (API-8).
    endpoint_url: str | None = None
    identity_type: ModelIdentity = ModelIdentity.NONE
    secret_ref: str | None = None
    identity_config: ModelIdentityConfig = Field(default_factory=ModelIdentityConfig)

    @field_validator("endpoint_url")
    @classmethod
    def _validate_endpoint(cls, value: str | None) -> str | None:
        return None if value is None else _validate_endpoint_url(value)


class ModelUpdate(BaseSchema):
    """Partial update payload for a model; all fields optional."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    task: ModelTask | None = None
    endpoint_url: str | None = None
    identity_type: ModelIdentity | None = None
    secret_ref: str | None = None
    identity_config: ModelIdentityConfig | None = None

    @field_validator("endpoint_url")
    @classmethod
    def _validate_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return value
        return _validate_endpoint_url(value)


class ModelRead(BaseSchema):
    """Model as returned by the API; `secret_ref` is never exposed, only its presence."""

    id: UUID
    organization_id: UUID
    name: str
    task: ModelTask
    endpoint_url: str | None
    identity_type: ModelIdentity
    has_secret: bool
    identity_config: ModelIdentityConfig
    created_at: datetime


class ModelDerivation(StrEnum):
    """How a version came from its parent (EXP-8)."""

    TRAINED = "trained"
    DISTILLED = "distilled"
    QUANTIZED = "quantized"


class ModelVersionCreate(BaseSchema):
    """Payload to add a version to a model.

    `version` defaults to the next integer after the model's current
    highest version (starting at 1) when omitted.
    """

    version: int | None = Field(default=None, ge=1)
    class_mapping: dict[str, str | None] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    #: Lineage (EXP-8): the snapshot the version was trained on and its
    #: digest; the server checks the two agree. `training_run` is stored as-is.
    snapshot_id: UUID | None = None
    snapshot_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    training_run: dict[str, Any] | None = None
    #: Derivation (EXP-8): the version this one was trained, distilled or
    #: quantized from, in any model of the organisation.
    parent_version_id: UUID | None = None
    derivation: ModelDerivation | None = None

    @model_validator(mode="after")
    def _derived_needs_parent(self) -> ModelVersionCreate:
        derived = (ModelDerivation.DISTILLED, ModelDerivation.QUANTIZED)
        if self.derivation in derived and self.parent_version_id is None:
            raise ValueError(f"a {self.derivation} version needs parent_version_id")
        return self


class ModelVersionRead(BaseSchema):
    """A model version as returned by the API."""

    id: UUID
    model_id: UUID
    version: int
    class_mapping: dict[str, str | None]
    metrics: dict[str, Any]
    snapshot_id: UUID | None = None
    snapshot_digest: str | None = None
    training_run: dict[str, Any] | None = None
    parent_version_id: UUID | None = None
    derivation: ModelDerivation | None = None
    created_at: datetime


class ModelFamilyVersion(ModelVersionRead):
    """A version in a derivation graph, with the model it belongs to (EXP-8)."""

    model_name: str
    model_task: ModelTask


class ModelFamily(BaseSchema):
    """Every version connected to a model's versions through parent links."""

    versions: list[ModelFamilyVersion]


class ModelCheckResult(BaseSchema):
    """Result of testing a model endpoint's connectivity (BYOM-3)."""

    ok: bool
    messages: list[str]
    info: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Interactive segmentation (ML-7)
# --------------------------------------------------------------------------- #


class InteractivePoint(BaseSchema):
    """A click, in original-image pixels."""

    x: float = Field(ge=0)
    y: float = Field(ge=0)


class InteractiveRequest(BaseSchema):
    """`POST /items/{id}/interactive`: one prompt for a `segment` model.

    Exactly one of `point` / `box` — the model turns it into a polygon. The
    text prompt the reference service also understands is deliberately not
    exposed yet: nothing in the annotator asks for it.
    """

    model_id: UUID
    point: InteractivePoint | None = None
    box: tuple[float, float, float, float] | None = None

    @model_validator(mode="after")
    def _one_prompt(self) -> InteractiveRequest:
        if (self.point is None) == (self.box is None):
            raise ValueError("give exactly one of 'point' or 'box'")
        if self.box is not None:
            x_min, y_min, x_max, y_max = self.box
            if x_max <= x_min or y_max <= y_min:
                raise ValueError("box must be [x_min, y_min, x_max, y_max] with a positive area")
        return self


class InteractiveResult(BaseSchema):
    """The polygon the model proposed; the browser adds it as a normal shape."""

    type: Literal["polygon"] = "polygon"
    points: list[tuple[float, float]]
    confidence: float


class OcrRequest(BaseSchema):
    """`POST /items/{id}/ocr`: the words of one page of a scanned PDF."""

    model_id: UUID
    page: int = Field(ge=1, description="1-based page number")


class OcrWord(BaseSchema):
    """One word; `bbox` `[x_min, y_min, x_max, y_max]` in the page's points."""

    text: str
    bbox: tuple[float, float, float, float]


class OcrResult(BaseSchema):
    """The words an `ocr` model read on a page. Nothing is stored."""

    page: int
    width: float
    height: float
    engine: str
    words: list[OcrWord]


# --------------------------------------------------------------------------- #
# Correction metrics (ML-5)
# --------------------------------------------------------------------------- #


class CorrectionShapeCounts(BaseSchema):
    """Fate of a model version's shapes once a human finished the item."""

    #: Shapes the model drew.
    model: int
    #: Left exactly as drawn.
    kept: int
    #: Same class, geometry moved.
    adjusted: int
    #: Same shape, different class.
    relabeled: int
    #: Removed by the human.
    deleted: int
    #: Drawn by the human, missed by the model.
    added: int


class CorrectionClassMetrics(CorrectionShapeCounts):
    """The same counts for one class, with its precision / recall."""

    name: str
    #: `(kept + adjusted) / model`; null without model shapes.
    precision: float | None
    #: `(kept + adjusted) / final shapes of this class` (kept + adjusted + added +
    #: shapes relabeled into it); null without final shapes.
    recall: float | None
    #: Mean IoU of adjusted bounding boxes; null when none were adjusted.
    mean_iou_adjusted: float | None


class CorrectionMetrics(BaseSchema):
    """`GET /models/{id}/versions/{vid}/metrics` (ML-5)."""

    model_version_id: UUID
    project_id: UUID | None
    #: Items this version wrote a draft for.
    items_predicted: int
    #: ... of which a human has since submitted or had approved a later version.
    items_corrected: int
    items_pending: int
    #: Corrected items where the human changed nothing.
    items_accepted_unchanged: int
    shapes: CorrectionShapeCounts
    precision: float | None
    recall: float | None
    mean_iou_adjusted: float | None
    classes: list[CorrectionClassMetrics]
