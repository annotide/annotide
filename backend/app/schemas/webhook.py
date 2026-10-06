"""Request/response DTOs for webhooks (API-4) and retraining requests (ML-9)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import Field, HttpUrl, field_validator

from app.schemas.common import BaseSchema
from app.schemas.ml_platform import MlRun


class WebhookFormat(StrEnum):
    """What a hook receives (API-7): the signed event, or a chat message."""

    JSON = "json"
    SLACK = "slack"
    TEAMS = "teams"


class WebhookDeliveryStatus(StrEnum):
    """Lifecycle of one delivery."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


#: Every event the platform emits (API-4); `services/webhooks.py` emits them
#: and `POST /webhooks` validates subscriptions against this list.
EVENTS: tuple[str, ...] = (
    "annotation.submitted",
    "annotation.approved",
    "annotation.rejected",
    "item.approved",
    "snapshot.created",
    "job.succeeded",
    "job.failed",
    "retrain.requested",
    "webhook.test",
)
WILDCARD = "*"


def _validate_events(events: list[str]) -> list[str]:
    unknown = sorted(set(events) - set(EVENTS) - {WILDCARD})
    if unknown:
        raise ValueError(f"unknown events: {', '.join(unknown)}; known: {', '.join(EVENTS)}")
    return sorted(set(events))


class WebhookCreate(BaseSchema):
    """`POST /webhooks`: subscribe a URL to events.

    `project_id` null subscribes to every project in the organisation
    (superuser only); set, the caller must own that project. `events` names
    the events to receive, or `["*"]` for all of them.
    """

    url: HttpUrl
    events: list[str] = Field(min_length=1)
    project_id: UUID | None = None
    description: str | None = Field(default=None, max_length=500)
    is_active: bool = True
    #: `slack` / `teams`: the URL is the channel's incoming webhook (API-7).
    format: WebhookFormat = WebhookFormat.JSON

    @field_validator("events")
    @classmethod
    def _events_known(cls, value: list[str]) -> list[str]:
        return _validate_events(value)


class WebhookUpdate(BaseSchema):
    """`PATCH /webhooks/{id}`: only keys present change. `rotate_secret: true`
    replaces the signing secret and returns the new one once."""

    url: HttpUrl | None = None
    events: list[str] | None = Field(default=None, min_length=1)
    description: str | None = None
    is_active: bool | None = None
    format: WebhookFormat | None = None
    rotate_secret: bool = False

    @field_validator("events")
    @classmethod
    def _events_known(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _validate_events(value)


class WebhookRead(BaseSchema):
    """A webhook as returned by the API; the secret is never included."""

    id: UUID
    organization_id: UUID
    project_id: UUID | None
    url: str
    description: str | None
    events: list[str]
    is_active: bool
    format: WebhookFormat = WebhookFormat.JSON
    created_by_id: UUID | None
    last_delivery_at: datetime | None
    last_response_status: int | None
    created_at: datetime
    updated_at: datetime


class WebhookCreated(WebhookRead):
    """The freshly created (or rotated) webhook, carrying `secret` exactly once."""

    secret: str


class WebhookUpdated(WebhookRead):
    """`PATCH` response: `secret` is set only when it was rotated in this call."""

    secret: str | None = None


class WebhookDeliveryRead(BaseSchema):
    """One delivery, for the per-hook delivery log."""

    id: UUID
    webhook_id: UUID
    event: str
    payload: dict[str, Any]
    status: WebhookDeliveryStatus
    attempts: int
    next_attempt_at: datetime
    response_status: int | None
    error: str | None
    delivered_at: datetime | None
    created_at: datetime


class RetrainRequest(BaseSchema):
    """`POST /projects/{id}/retrain` (ML-9): ask subscribers to train.

    Everything is optional context for the training pipeline; the platform
    does not train anything itself.
    """

    snapshot_id: UUID | None = None
    model_id: UUID | None = None
    note: str | None = Field(default=None, max_length=1000)
    #: A Databricks platform with `config.job_id`: start that job too (API-6).
    ml_platform_id: UUID | None = None


class RetrainResult(BaseSchema):
    """How many webhook deliveries the request queued, and the job run it started."""

    event: str
    deliveries: int
    ml_run: MlRun | None = None
