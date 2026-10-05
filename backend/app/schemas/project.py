"""Request/response DTOs for the project entity."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID

from pydantic import AfterValidator, ConfigDict, Field, model_validator

from app.schemas.common import BaseSchema


class ReviewMode(StrEnum):
    """Whether submitted work goes through a reviewer (WF-1)."""

    REQUIRED = "required"
    NONE = "none"
    #: QA-7: a deterministic sample of submissions is reviewed, the rest approve.
    SAMPLED = "sampled"


class RejectionTarget(StrEnum):
    """Who gets the annotate task a rejection opens (WF-1)."""

    SAME_ANNOTATOR = "same_annotator"
    QUEUE = "queue"


class WorkflowConfig(BaseSchema):
    """`project.workflow`, see CONTRACTS.md *Project workflow JSON* (WF-1).

    Every field defaults to the standard annotate → review → approve flow, so
    `{}` and pre-existing rows behave as before. Unknown keys are refused so a
    typo cannot silently leave the default in force.
    """

    model_config = ConfigDict(extra="forbid")

    review: ReviewMode = ReviewMode.REQUIRED
    rejection_returns_to: RejectionTarget = RejectionTarget.SAME_ANNOTATOR
    allow_skip: bool = True
    allow_self_review: bool = True
    consensus_annotators: int = Field(default=1, ge=1, le=10)
    #: QA-7: the share of items reviewed under `review: sampled`.
    review_sample_rate: float = Field(default=0.1, gt=0.0, le=1.0)
    #: QA-4: hand an annotator a gold task after every `gold_every - 1` others.
    gold_every: int | None = Field(default=None, ge=2, le=1000)

    @model_validator(mode="after")
    def _consensus_needs_review(self) -> WorkflowConfig:
        # N parallel versions need someone to resolve them (QA-1, QA-3); a
        # sample would leave most of them unresolved.
        if self.consensus_annotators > 1 and self.review is not ReviewMode.REQUIRED:
            raise ValueError("consensus_annotators > 1 requires review: required")
        return self


class Calibration(BaseSchema):
    """`settings.calibration` (TOOL-8): the physical size of one image pixel."""

    units_per_pixel: float = Field(gt=0, allow_inf_nan=False)
    unit: str = Field(min_length=1, max_length=16)


_ROLES = frozenset({"owner", "reviewer", "annotator", "viewer"})


def _validate_idp_groups(value: object) -> dict[str, str]:
    """`settings.idp_groups` (AUTH-3): IdP group → project role."""
    if not isinstance(value, dict):
        raise ValueError("idp_groups must be an object mapping IdP groups to roles")
    for group, role in value.items():
        if not isinstance(group, str) or not 1 <= len(group) <= 255:
            raise ValueError("idp_groups keys must be 1-255 characters")
        if role not in _ROLES:
            raise ValueError(f"idp_groups[{group!r}] must be one of {sorted(_ROLES)}")
    return dict(value)


_EXTENSION = re.compile(r"^\.[a-z0-9]{1,10}$")


def _validate_companion_extensions(value: Any) -> list[str]:
    """`settings.companion_extensions` (§5 multimodal): 1-10 lower-case `.ext` strings."""
    if not isinstance(value, list) or not 1 <= len(value) <= 10:
        raise ValueError("companion_extensions must be a list of 1-10 extensions")
    for extension in value:
        if not isinstance(extension, str) or not _EXTENSION.fullmatch(extension):
            raise ValueError(f"companion extension {extension!r} must look like '.txt'")
    return sorted(set(value))


def _validate_pdf_mode(value: Any) -> str:
    """`settings.pdf_mode` (PDF text mode): `layout` or `text`."""
    if value not in ("layout", "text"):
        raise ValueError("pdf_mode must be 'layout' or 'text'")
    return str(value)


def _validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Check the keys of `project.settings` that have a defined shape; the rest is free-form."""
    if settings.get("companion_extensions") is not None:
        settings = {
            **settings,
            "companion_extensions": _validate_companion_extensions(
                settings["companion_extensions"]
            ),
        }
    if settings.get("pdf_mode") is not None:
        settings = {**settings, "pdf_mode": _validate_pdf_mode(settings["pdf_mode"])}
    if settings.get("idp_groups") is not None:
        settings = {**settings, "idp_groups": _validate_idp_groups(settings["idp_groups"])}
    if settings.get("calibration") is not None:
        settings = {
            **settings,
            "calibration": Calibration.model_validate(settings["calibration"]).model_dump(),
        }
    return settings


#: `project.settings`: free-form JSON except for the keys checked above.
ProjectSettings = Annotated[dict[str, Any], AfterValidator(_validate_settings)]


class ProjectCreate(BaseSchema):
    """Payload to create a new project (organization id comes from the auth context)."""

    name: str
    description: str | None = None
    label_schema_id: UUID | None = None
    source_connector_id: UUID | None = None
    result_connector_id: UUID | None = None
    #: Derived data (`cache/`) container; `None` = the result connector (SRC-6).
    cache_connector_id: UUID | None = None
    source_prefix: str | None = None
    source_glob: str | None = None
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    settings: ProjectSettings = Field(default_factory=dict)


class ProjectUpdate(BaseSchema):
    """Partial update payload for a project; all fields optional."""

    name: str | None = None
    description: str | None = None
    label_schema_id: UUID | None = None
    source_connector_id: UUID | None = None
    result_connector_id: UUID | None = None
    #: Derived data (`cache/`) container; `None` = the result connector (SRC-6).
    cache_connector_id: UUID | None = None
    source_prefix: str | None = None
    source_glob: str | None = None
    workflow: WorkflowConfig | None = None
    settings: ProjectSettings | None = None


class ProjectRead(BaseSchema):
    """Project as returned by the API."""

    id: UUID
    organization_id: UUID
    name: str
    description: str | None
    label_schema_id: UUID | None
    source_connector_id: UUID | None
    result_connector_id: UUID | None
    cache_connector_id: UUID | None
    source_prefix: str | None
    source_glob: str | None
    workflow: WorkflowConfig
    settings: dict[str, Any]
    created_at: datetime
    updated_at: datetime
