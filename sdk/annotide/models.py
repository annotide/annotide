"""Wire types of the Annotide API — generated, do not edit.

Regenerate with `make openapi` from the repository root. Every model is a
`TypedDict`: API responses are plain dicts, typed for editors and mypy.
"""

from __future__ import annotations
from typing import Any, Literal, NotRequired, TypedDict


class AgreementAnnotator(TypedDict):
    """
    One annotator's footprint in a `ProjectAgreement` (how many items they touched).
    """

    display_name: str
    email: str
    items: int
    user_id: str


type AnnotationKind = Literal["primary", "consensus", "gold"]


class AnnotationRead(TypedDict):
    """
    One annotation version as returned by the API.
    """

    author_model_version_id: str | None
    author_user_id: str | None
    blob_path: NotRequired[str | None]
    duration_ms: int | None
    id: str
    item_id: str
    kind: NotRequired[AnnotationKind]
    label_schema_version_id: str
    result: dict[str, Any]
    source: str
    status: str
    task_id: str | None
    version: int


type AnnotationSource = Literal["human", "model"]


class AnnotationStats(TypedDict):
    by_source: dict[str, int]
    latest_by_status: dict[str, int]
    versions: int


type AnnotationStatus = Literal["draft", "submitted", "approved", "rejected"]


class AnnotatorQuality(TypedDict):
    """
    One annotator's accuracy against gold references (QA-4).
    """

    classification_accuracy: float | None
    display_name: str
    email: str
    gold_items: int
    mean_iou: float | None
    score: float | None
    shape_f1: float | None
    span_f1: float | None
    user_id: str


class AnnotatorQualityResponse(TypedDict):
    """
    `GET /projects/{id}/quality/annotators` response body (QA-4).
    """

    annotators: list[AnnotatorQuality]


class AnnotatorStats(TypedDict):
    approved: NotRequired[int]
    display_name: str
    rejected: NotRequired[int]
    submitted: NotRequired[int]
    user_id: str


class ApiKeyCreate(TypedDict):
    """
    Payload to mint an API key (AUTH-4).

    `scopes` is a subset of `read` / `write` / `admin`; `user_id` (superuser
    only) issues the key for another user or a service account.
    """

    expires_at: NotRequired[str | None]
    name: str
    scopes: NotRequired[list[str]]
    user_id: NotRequired[str | None]


class ApiKeyCreated(TypedDict):
    """
    The freshly minted key, carrying the bearer `token` exactly once.
    """

    created_at: str
    created_by: str | None
    expires_at: str | None
    id: str
    last_used_at: str | None
    name: str
    organization_id: str
    revoked_at: str | None
    scopes: list[str]
    token: str
    user_id: str


class ApiKeyRead(TypedDict):
    """
    API key metadata; the token itself is only returned at creation.
    """

    created_at: str
    created_by: str | None
    expires_at: str | None
    id: str
    last_used_at: str | None
    name: str
    organization_id: str
    revoked_at: str | None
    scopes: list[str]
    user_id: str


type AttributeType = Literal["text", "number", "select", "multiselect", "boolean"]


class AuditEventRead(TypedDict):
    """
    One append-only audit row.
    """

    action: str
    actor_id: str | None
    after: dict[str, Any] | None
    before: dict[str, Any] | None
    created_at: str
    id: str
    ip: str | None
    organization_id: str
    target_id: str | None
    target_type: str


BBoxShape = TypedDict(
    "BBoxShape",
    {
        "attributes": NotRequired[dict[str, Any]],
        "bbox": tuple[float, float, float, float],
        "class": str,
        "confidence": NotRequired[float | None],
        "frame": NotRequired[int | None],
        "id": str,
        "keyframe": NotRequired[bool],
        "outside": NotRequired[bool],
        "page": NotRequired[int | None],
        "text": NotRequired[str | None],
        "track_id": NotRequired[str | None],
        "type": NotRequired[Literal["bbox"]],
    },
)


class BulkApprove(TypedDict):
    """
    Approve the latest `submitted` version of each item (WF-4 verdict
    without a correction). Items not awaiting review are skipped.
    """

    action: Literal["approve"]
    comment: NotRequired[str | None]
    item_ids: list[str]


class BulkReject(TypedDict):
    """
    Reject the latest `submitted` version of each item (WF-4) with one
    shared `comment`, which every item's thread gets — a rejection always
    tells the annotator why. Items not awaiting review are skipped.
    """

    action: Literal["reject"]
    comment: str
    item_ids: list[str]


class BulkReturn(TypedDict):
    """
    Return the items' `in_progress` tasks to the open queue: the lock is
    dropped and the assignee cleared, as if the holder had released it.
    """

    action: Literal["return"]
    item_ids: list[str]


class BulkSkipped(TypedDict):
    """
    One item the action did not apply to, and why.
    """

    item_id: str
    reason: str


class BulkTag(TypedDict):
    """
    Add and / or remove free-form tags on the items (`item.meta.tags`).
    """

    action: Literal["tag"]
    add: NotRequired[list[str]]
    item_ids: list[str]
    remove: NotRequired[list[str]]


class CacheRebuildRequest(TypedDict):
    """
    Body of `POST /projects/{id}/cache/rebuild` (SRC-6).
    """

    purge: NotRequired[bool]


class ClassCount(TypedDict):
    count: int
    label: str


class ClassificationAgreement(TypedDict):
    """
    Agreement for one classification field, pooled over items with a value (QA-2).
    """

    field: str
    fleiss_kappa: float | None
    items: int
    krippendorff_alpha: float | None


class CommentCreate(TypedDict):
    """
    Payload to add a comment to an item's thread.
    """

    anchor: NotRequired[dict[str, Any] | None]
    annotation_id: NotRequired[str | None]
    body: str
    parent_id: NotRequired[str | None]


class CommentRead(TypedDict):
    """
    Comment as returned by the API.
    """

    anchor: dict[str, Any] | None
    annotation_id: str | None
    author_id: str
    body: str
    created_at: str
    id: str
    item_id: str | None
    parent_id: str | None
    project_id: str
    resolved_at: str | None
    updated_at: str


class CommentResolve(TypedDict):
    """
    Payload to resolve or reopen a comment.
    """

    resolved: bool


class ConnectorCheckResult(TypedDict):
    """
    Result of testing a connector's connectivity (SRC-7).
    """

    messages: list[str]
    ok: bool


type ConnectorIdentity = Literal[
    "managed_identity",
    "service_principal",
    "account_key",
    "sas_token",
    "iam_role",
    "access_key",
    "none",
]


type ConnectorType = Literal[
    "azure_blob", "s3", "gcs", "local", "http", "sharepoint", "databricks_volume"
]


class ConnectorUpdate(TypedDict):
    """
    Partial update payload for a connector; all fields optional.
    """

    config: NotRequired[dict[str, Any] | None]
    identity_type: NotRequired[ConnectorIdentity | None]
    name: NotRequired[str | None]
    secret_ref: NotRequired[str | None]
    type: NotRequired[ConnectorType | None]


class ConsensusAnnotationRead(TypedDict):
    """
    The annotation version `resolve` stores, mirroring `annotations.py`'s `AnnotationRead`.
    """

    author_model_version_id: str | None
    author_user_id: str | None
    blob_path: NotRequired[str | None]
    duration_ms: int | None
    id: str
    item_id: str
    label_schema_version_id: str
    result: dict[str, Any]
    source: str
    status: str
    task_id: str | None
    version: int


class ConsensusAnnotatorRead(TypedDict):
    """
    One consensus annotator's latest submitted version on an item.
    """

    annotation_id: str
    created_at: str
    display_name: str
    email: str
    status: str
    user_id: str
    version: int


class CorrectionClassMetrics(TypedDict):
    """
    The same counts for one class, with its precision / recall.
    """

    added: int
    adjusted: int
    deleted: int
    kept: int
    mean_iou_adjusted: float | None
    model: int
    name: str
    precision: float | None
    recall: float | None
    relabeled: int


class CorrectionShapeCounts(TypedDict):
    """
    Fate of a model version's shapes once a human finished the item.
    """

    added: int
    adjusted: int
    deleted: int
    kept: int
    model: int
    relabeled: int


class DependencyStatus(TypedDict):
    detail: NotRequired[str | None]
    ok: bool


class EraseRequest(TypedDict):
    """
    `confirm_email` must repeat the user's current e-mail, as a guard.
    """

    confirm_email: str
    redact_comments: NotRequired[bool]


class EventDeliveryResult(TypedDict):
    """
    What one storage-event delivery queued (SRC-3).
    """

    ignored: int
    job_ids: list[str]
    matched: int
    received: int


class EventTokenRead(TypedDict):
    """
    A freshly minted storage-event token (SRC-3), shown once.
    """

    path: str
    token: str


class ExportDownload(TypedDict):
    """
    A short-lived signed URL for a succeeded export's archive (EXP-5).
    """

    expires_in: int
    url: str


class ExtendRequest(TypedDict):
    """
    Optional JSON body for `POST /tasks/{task_id}/extend`.
    """

    lock_ttl_seconds: NotRequired[int | None]


class ExtractTextRequest(TypedDict):
    """
    Body of `POST /projects/{id}/extract-text` (CONTRACTS.md *PDF text mode*).
    """

    force: NotRequired[bool]
    item_ids: NotRequired[list[str] | None]


class FuseResolve(TypedDict):
    """
    Resolve by fusing every submitted consensus version (QA-3).
    """

    comment: NotRequired[str | None]
    iou_threshold: NotRequired[float]
    method: NotRequired[Literal["fuse"]]
    min_votes: NotRequired[int | None]


class GoldSetRequest(TypedDict):
    """
    `PUT /items/{id}/gold` payload: an approved primary version of this item.
    """

    annotation_id: str


class GoldTasksRequest(TypedDict):
    """
    `POST /projects/{id}/gold/tasks` payload.

    Defaults (both omitted) to every member with role `annotator` times every
    item with a gold reference.
    """

    item_ids: NotRequired[list[str] | None]
    priority: NotRequired[int]
    user_ids: NotRequired[list[str] | None]


class GoldTasksResult(TypedDict):
    """
    How many (item, user) gold tasks a `gold/tasks` request opened or skipped.
    """

    opened: int
    skipped: int


class HealthResponse(TypedDict):
    status: Literal["ok"]


type ImportStatus = Literal["submitted", "draft"]


class InteractivePoint(TypedDict):
    """
    A click, in original-image pixels.
    """

    x: float
    y: float


class InteractiveRequest(TypedDict):
    """
    `POST /items/{id}/interactive`: one prompt for a `segment` model.

    Exactly one of `point` / `box` — the model turns it into a polygon. The
    text prompt the reference service also understands is deliberately not
    exposed yet: nothing in the annotator asks for it.
    """

    box: NotRequired[tuple[float, float, float, float] | None]
    model_id: str
    point: NotRequired[InteractivePoint | None]


class InteractiveResult(TypedDict):
    """
    The polygon the model proposed; the browser adds it as a normal shape.
    """

    confidence: float
    points: list[tuple[float, float]]
    type: NotRequired[Literal["polygon"]]


class ItemStats(TypedDict):
    by_status: dict[str, int]
    total: int


type ItemStatusOutput = Literal[
    "new",
    "prelabeled",
    "annotating",
    "submitted",
    "in_review",
    "approved",
    "rejected",
    "skipped",
]


type JobStatusInput = Literal["queued", "running", "succeeded", "failed", "cancelled"]


type JobStatusOutput = Literal["queued", "running", "succeeded", "failed", "cancelled"]


type JobTypeInput = Literal[
    "scan_source",
    "tile_image",
    "prelabel",
    "export",
    "snapshot",
    "import",
    "thumbnail",
    "rebuild_cache",
    "extract_text",
]


type JobTypeOutput = Literal[
    "scan_source",
    "tile_image",
    "prelabel",
    "export",
    "snapshot",
    "import",
    "thumbnail",
    "rebuild_cache",
    "extract_text",
]


KeypointsShape = TypedDict(
    "KeypointsShape",
    {
        "attributes": NotRequired[dict[str, Any]],
        "class": str,
        "confidence": NotRequired[float | None],
        "frame": NotRequired[int | None],
        "id": str,
        "keyframe": NotRequired[bool],
        "outside": NotRequired[bool],
        "page": NotRequired[int | None],
        "points": list[tuple[float, float, int]],
        "track_id": NotRequired[str | None],
        "type": NotRequired[Literal["keypoints"]],
    },
)


class LabelSchemaVersionRead(TypedDict):
    """
    One immutable version of a project's label schema (TOOL-4).
    """

    created_at: str
    definition: dict[str, Any]
    id: str
    label_schema_id: str
    version: int


class LicenseInfo(TypedDict):
    """
    Licence status as seen by an administrator.

    ``tier`` is the edition (LIC-32): without a key in force (or with an
    invalid one) it is ``"community"`` and the key's fields are ``None``.
    ``seat_limit`` includes the overage and is ``None`` when the build
    enforces no limit (LIC-23, LIC-24).
    """

    active_users: int
    business_features: list[str]
    expires_at: str | None
    features: list[str]
    grace_ends_at: str | None
    host_mismatch: bool
    hosts: list[str]
    license_id: str | None
    licensee: str | None
    owner_only: bool
    restricted: bool
    revoked_at: str | None
    seat_limit: int | None
    seats: int | None
    source: str | None
    status: str
    tier: str
    trial_used: bool


class LicenseKeyUpdate(TypedDict):
    """
    `PUT /license` body: a key to keep in the database (LIC-26).
    """

    key: str


class LicenseRefreshStatus(TypedDict):
    """
    `GET` / `POST /license/refresh` (LIC-27): what is sent, and how the last try went.
    """

    attempted_at: str | None
    enabled: bool
    error: str | None
    last_payload: dict[str, Any] | None
    payload: dict[str, Any] | None
    server_configured: bool
    succeeded_at: str | None


class LoginRequest(TypedDict):
    """
    Local login credentials (AUTH-2).
    """

    email: str
    otp: NotRequired[str | None]
    password: str


class MaskRLE(TypedDict):
    """
    Uncompressed run-length encoding of a binary mask (COCO-style: alternating

    background/foreground run lengths, starting with background).
    """

    counts: list[int]
    size: tuple[int, int]


MaskShape = TypedDict(
    "MaskShape",
    {
        "attributes": NotRequired[dict[str, Any]],
        "class": str,
        "confidence": NotRequired[float | None],
        "frame": NotRequired[int | None],
        "id": str,
        "keyframe": NotRequired[bool],
        "outside": NotRequired[bool],
        "page": NotRequired[int | None],
        "rle": MaskRLE,
        "track_id": NotRequired[str | None],
        "type": NotRequired[Literal["mask"]],
    },
)


type MediaType = Literal["image", "video", "audio", "text", "pdf", "llm", "timeseries"]


class MfaCode(TypedDict):
    """
    A six-digit code from the authenticator app, or a recovery code where allowed.
    """

    code: str


class MfaRecoveryCodes(TypedDict):
    """
    Single-use recovery codes, shown once.
    """

    recovery_codes: list[str]


class MfaSetup(TypedDict):
    """
    `POST /auth/mfa/setup`: the seed to add to an authenticator app.
    """

    otpauth_uri: str
    secret: str


class MfaStatus(TypedDict):
    """
    `GET /auth/mfa`: the caller's MFA state (AUTH-2).
    """

    available: bool
    enabled: bool
    pending: bool
    recovery_codes_left: int


type MlIdentity = Literal[
    "none", "bearer", "basic", "service_principal", "managed_identity"
]


class MlPlatformCheckResult(TypedDict):
    """
    `POST /ml-platforms/{id}/check`: failures are `ok: false`, never an error status.
    """

    info: NotRequired[dict[str, Any] | None]
    messages: list[str]
    ok: bool


type MlPlatformKind = Literal["mlflow", "databricks", "azureml"]


class MlPlatformRead(TypedDict):
    """
    A platform as the API returns it: `secret_ref` never, only `has_secret`.
    """

    config: dict[str, Any]
    created_at: str
    has_secret: bool
    id: str
    identity_type: MlIdentity
    kind: MlPlatformKind
    name: str
    organization_id: str
    tracking_uri: str
    updated_at: str


class MlPlatformUpdate(TypedDict):
    """
    Partial update; the merged row is validated as a whole.
    """

    config: NotRequired[dict[str, Any] | None]
    identity_type: NotRequired[MlIdentity | None]
    name: NotRequired[str | None]
    secret_ref: NotRequired[str | None]
    tracking_uri: NotRequired[str | None]


class MlRun(TypedDict):
    """
    A run the platform started on an ML platform (retrain on Databricks).
    """

    ml_platform_id: str
    run_id: str
    run_url: str | None


class ModelCheckResult(TypedDict):
    """
    Result of testing a model endpoint's connectivity (BYOM-3).
    """

    info: NotRequired[dict[str, Any] | None]
    messages: list[str]
    ok: bool


type ModelDerivation = Literal["trained", "distilled", "quantized"]


type ModelIdentity = Literal[
    "none", "api_key", "bearer", "service_principal", "managed_identity"
]


class ModelIdentityConfig(TypedDict):
    """
    The non-secret half of an Entra identity (BYOM-3).

    `scope` is the token audience, e.g. `https://ml.azure.com/.default` for an
    Azure ML online endpoint or `https://cognitiveservices.azure.com/.default`
    for Azure OpenAI / AI Foundry. `tenant_id` and `client_id` name a service
    principal; for a managed identity, `client_id` picks a user-assigned one
    (omitted: the system-assigned or workload identity).
    """

    client_id: NotRequired[str | None]
    scope: NotRequired[str | None]
    tenant_id: NotRequired[str | None]


type ModelTask = Literal["detect", "segment", "classify", "ner", "llm", "ocr"]


class ModelUpdate(TypedDict):
    """
    Partial update payload for a model; all fields optional.
    """

    endpoint_url: NotRequired[str | None]
    identity_config: NotRequired[ModelIdentityConfig | None]
    identity_type: NotRequired[ModelIdentity | None]
    name: NotRequired[str | None]
    secret_ref: NotRequired[str | None]
    task: NotRequired[ModelTask | None]


class ModelVersionCreate(TypedDict):
    """
    Payload to add a version to a model.

    `version` defaults to the next integer after the model's current
    highest version (starting at 1) when omitted.
    """

    class_mapping: NotRequired[dict[str, str | None]]
    derivation: NotRequired[ModelDerivation | None]
    metrics: NotRequired[dict[str, Any]]
    parent_version_id: NotRequired[str | None]
    snapshot_digest: NotRequired[str | None]
    snapshot_id: NotRequired[str | None]
    training_run: NotRequired[dict[str, Any] | None]
    version: NotRequired[int | None]


class ModelVersionImport(TypedDict):
    """
    `POST /models/{id}/versions/import`: a version from an MLflow run.

    Name the run directly, or a registered model version whose run is read.
    """

    class_mapping: NotRequired[dict[str, str | None]]
    ml_platform_id: str
    model_version: NotRequired[str | None]
    registered_model: NotRequired[str | None]
    run_id: NotRequired[str | None]
    version: NotRequired[int | None]


class ModelVersionRead(TypedDict):
    """
    A model version as returned by the API.
    """

    class_mapping: dict[str, str | None]
    created_at: str
    derivation: NotRequired[ModelDerivation | None]
    id: str
    metrics: dict[str, Any]
    model_id: str
    parent_version_id: NotRequired[str | None]
    snapshot_digest: NotRequired[str | None]
    snapshot_id: NotRequired[str | None]
    training_run: NotRequired[dict[str, Any] | None]
    version: int


type NotificationType = Literal["mention", "reply", "review"]


class OcrRequest(TypedDict):
    """
    `POST /items/{id}/ocr`: the words of one page of a scanned PDF.
    """

    model_id: str
    page: int


class OcrWord(TypedDict):
    """
    One word; `bbox` `[x_min, y_min, x_max, y_max]` in the page's points.
    """

    bbox: tuple[float, float, float, float]
    text: str


class OidcProviderInfo(TypedDict):
    """
    The configured single sign-on provider, as the login page sees it (AUTH-1).
    """

    display_name: str
    login_path: NotRequired[str]


class OrganizationalUseNotice(TypedDict):
    """
    Whether this Community install looks like it is being used by an organisation.
    """

    looks_organizational: bool
    message: str | None
    reasons: list[str]


class PageAuditEventRead(TypedDict):
    items: list[AuditEventRead]
    next_cursor: NotRequired[str | None]


class PageMlPlatformRead(TypedDict):
    items: list[MlPlatformRead]
    next_cursor: NotRequired[str | None]


class PageModelVersionRead(TypedDict):
    items: list[ModelVersionRead]
    next_cursor: NotRequired[str | None]


class PairAgreement(TypedDict):
    """
    Agreement between one pair of annotators.

    `items` is `None` on `ItemAgreement` (one item; the count would always be
    1) and the number of shared items on `ProjectAgreement`.
    """

    a: str
    b: str
    cohen_kappa: float | None
    items: NotRequired[int | None]
    mean_iou: float | None
    shape_f1: float | None
    span_f1_exact: float | None
    span_f1_overlap: float | None


class PersonalAnnotation(TypedDict):
    """
    Metadata only: the result is project content, not personal data.
    """

    created_at: str
    duration_ms: int | None
    id: str
    item_id: str
    kind: str
    status: str
    version: int


class PersonalApiKey(TypedDict):
    created_at: str
    expires_at: str | None
    id: str
    last_used_at: str | None
    name: str
    revoked_at: str | None
    scopes: list[str]


class PersonalAuditEvent(TypedDict):
    action: str
    created_at: str
    id: str
    ip: str | None
    target_id: str | None
    target_type: str


class PersonalComment(TypedDict):
    annotation_id: str | None
    body: str
    created_at: str
    id: str
    item_id: str | None
    project_id: str
    resolved_at: str | None


class PersonalMembership(TypedDict):
    created_at: str
    project_id: str
    project_name: str
    role: str


class PersonalNotification(TypedDict):
    created_at: str
    id: str
    payload: dict[str, Any]
    read_at: str | None
    type: str


class PersonalProfile(TypedDict):
    """
    The user row without credentials: flags stand in for secrets.
    """

    created_at: str
    display_name: str
    email: str
    erased_at: str | None
    id: str
    idp_linked: bool
    is_active: bool
    is_service: bool
    is_superuser: bool
    last_seen_at: str | None
    mfa_enabled: bool
    organization_id: str
    updated_at: str


class PersonalTask(TypedDict):
    id: str
    item_id: str
    project_id: str
    status: str
    type: str


class PickResolve(TypedDict):
    """
    Resolve by copying one annotator's submitted consensus version verbatim.
    """

    annotation_id: str
    method: NotRequired[Literal["pick"]]


PointShape = TypedDict(
    "PointShape",
    {
        "attributes": NotRequired[dict[str, Any]],
        "class": str,
        "confidence": NotRequired[float | None],
        "frame": NotRequired[int | None],
        "id": str,
        "keyframe": NotRequired[bool],
        "outside": NotRequired[bool],
        "page": NotRequired[int | None],
        "point": tuple[float, float],
        "track_id": NotRequired[str | None],
        "type": NotRequired[Literal["point"]],
    },
)


PolygonShape = TypedDict(
    "PolygonShape",
    {
        "attributes": NotRequired[dict[str, Any]],
        "class": str,
        "confidence": NotRequired[float | None],
        "frame": NotRequired[int | None],
        "id": str,
        "keyframe": NotRequired[bool],
        "outside": NotRequired[bool],
        "page": NotRequired[int | None],
        "points": list[tuple[float, float]],
        "track_id": NotRequired[str | None],
        "type": NotRequired[Literal["polygon"]],
    },
)


PolylineShape = TypedDict(
    "PolylineShape",
    {
        "attributes": NotRequired[dict[str, Any]],
        "class": str,
        "confidence": NotRequired[float | None],
        "frame": NotRequired[int | None],
        "id": str,
        "keyframe": NotRequired[bool],
        "outside": NotRequired[bool],
        "page": NotRequired[int | None],
        "points": list[tuple[float, float]],
        "track_id": NotRequired[str | None],
        "type": NotRequired[Literal["polyline"]],
    },
)


type ProjectRole = Literal["owner", "annotator", "reviewer", "viewer"]


RBoxShape = TypedDict(
    "RBoxShape",
    {
        "angle": float,
        "attributes": NotRequired[dict[str, Any]],
        "center": tuple[float, float],
        "class": str,
        "confidence": NotRequired[float | None],
        "frame": NotRequired[int | None],
        "id": str,
        "keyframe": NotRequired[bool],
        "outside": NotRequired[bool],
        "page": NotRequired[int | None],
        "size": tuple[float, float],
        "track_id": NotRequired[str | None],
        "type": NotRequired[Literal["rbox"]],
    },
)


RankingShape = TypedDict(
    "RankingShape",
    {
        "attributes": NotRequired[dict[str, Any]],
        "class": str,
        "confidence": NotRequired[float | None],
        "frame": NotRequired[int | None],
        "id": str,
        "keyframe": NotRequired[bool],
        "order": list[str],
        "outside": NotRequired[bool],
        "page": NotRequired[int | None],
        "track_id": NotRequired[str | None],
        "type": NotRequired[Literal["ranking"]],
    },
)


RatingShape = TypedDict(
    "RatingShape",
    {
        "attributes": NotRequired[dict[str, Any]],
        "class": str,
        "confidence": NotRequired[float | None],
        "frame": NotRequired[int | None],
        "id": str,
        "keyframe": NotRequired[bool],
        "outside": NotRequired[bool],
        "page": NotRequired[int | None],
        "target": str,
        "track_id": NotRequired[str | None],
        "type": NotRequired[Literal["rating"]],
        "value": int,
    },
)


class ReadyResponse(TypedDict):
    checks: dict[str, DependencyStatus]
    status: Literal["ready", "degraded"]


type RejectionTarget = Literal["same_annotator", "queue"]


RelationShape = TypedDict(
    "RelationShape",
    {
        "attributes": NotRequired[dict[str, Any]],
        "class": str,
        "confidence": NotRequired[float | None],
        "frame": NotRequired[int | None],
        "from": str,
        "id": str,
        "keyframe": NotRequired[bool],
        "outside": NotRequired[bool],
        "page": NotRequired[int | None],
        "to": str,
        "track_id": NotRequired[str | None],
        "type": NotRequired[Literal["relation"]],
    },
)


class RetrainRequest(TypedDict):
    """
    `POST /projects/{id}/retrain` (ML-9): ask subscribers to train.

    Everything is optional context for the training pipeline; the platform
    does not train anything itself.
    """

    ml_platform_id: NotRequired[str | None]
    model_id: NotRequired[str | None]
    note: NotRequired[str | None]
    snapshot_id: NotRequired[str | None]


class RetrainResult(TypedDict):
    """
    How many webhook deliveries the request queued, and the job run it started.
    """

    deliveries: int
    event: str
    ml_run: NotRequired[MlRun | None]


type ReviewMode = Literal["required", "none", "sampled"]


class ReviewStats(TypedDict):
    approved: int
    rejected: int
    rejection_rate: float


class ScaleDef(TypedDict):
    """
    The integer scale of a `rating` class, e.g. 1-5 with named ends (§5 LLM-data).
    """

    labels: NotRequired[dict[str, str]]
    max: int
    min: int


class ScanRequest(TypedDict):
    """
    Body for queuing a source-scan job (SRC-2).

    Both fields are optional overrides of the project's own `source_prefix`
    / `source_glob`; an empty body scans with the project's defaults.
    """

    glob: NotRequired[str | None]
    prefix: NotRequired[str | None]


class ScimTokenRead(TypedDict):
    """
    A freshly minted SCIM token, shown once, and the SCIM base path.
    """

    path: str
    token: str


class ScimTokenState(TypedDict):
    enabled: bool


class SeatReportPeriod(TypedDict):
    """
    Seat use in one calendar month, clipped to the report range (LIC-30).
    """

    active_users: int
    end: str
    overage: int | None
    peak_active_users: int
    peak_at: str | None
    start: str


SegmentShape = TypedDict(
    "SegmentShape",
    {
        "attributes": NotRequired[dict[str, Any]],
        "channels": NotRequired[list[str] | None],
        "class": str,
        "confidence": NotRequired[float | None],
        "end": float,
        "frame": NotRequired[int | None],
        "id": str,
        "keyframe": NotRequired[bool],
        "outside": NotRequired[bool],
        "page": NotRequired[int | None],
        "speaker": NotRequired[str | None],
        "start": float,
        "text": NotRequired[str | None],
        "track_id": NotRequired[str | None],
        "type": NotRequired[Literal["segment"]],
    },
)


class ServiceAccountCreate(TypedDict):
    """
    Payload to create a service account (AUTH-4); the e-mail is synthetic.
    """

    display_name: str


class ShapeAgreement(TypedDict):
    """
    Pooled shape agreement: mean IoU of matched pairs and shape-F1 (QA-2).
    """

    envelope_iou: NotRequired[bool]
    f1: float | None
    iou_threshold: float
    mean_iou: float | None


class SkeletonDef(TypedDict):
    """
    Named keypoints of a `keypoints` class and the bones between them (TOOL).

    The order of `points` is the order a `keypoints` shape stores them in.
    `edges` are 0-based index pairs into `points`.
    """

    edges: NotRequired[list[tuple[int, int]]]
    points: list[str]


class SkipRequest(TypedDict):
    """
    Payload for `POST /items/{item_id}/skip`; a reason is mandatory (TOOL-6).
    """

    reason: str


class SnapshotCreate(TypedDict):
    """
    Body for `POST /projects/{id}/snapshots`. Queues a snapshot job.

    `filter` is a dataset filter (`services/datasets.py::DatasetFilter`,
    documented in CONTRACTS.md → snapshot); empty freezes every annotated
    item at its latest version.
    """

    filter: NotRequired[dict[str, Any]]
    label_schema_version_id: NotRequired[str | None]
    name: str
    split: NotRequired[dict[str, Any] | None]


class SnapshotDiffChanged(TypedDict):
    """
    An item frozen at different annotation versions on the two sides.
    """

    from_version: int
    item_id: str
    path: str
    shapes: dict[str, int]
    to_version: int


class SnapshotDiffClass(TypedDict):
    """
    Shape count per class on each side; `delta` is target minus base.
    """

    base: int
    delta: int
    name: str
    target: int


class SnapshotDiffEntry(TypedDict):
    """
    An item present on only one side.
    """

    item_id: str
    path: str
    split: NotRequired[str | None]
    version: int


class SnapshotDiffItems(TypedDict):
    """
    Item-level totals; complete even when the lists below are truncated.
    """

    added: int
    changed: int
    removed: int
    split_moved: int
    unchanged: int


class SnapshotDiffSide(TypedDict):
    """
    One of the two snapshots being compared.
    """

    created_at: str
    digest: str
    id: str
    item_count: int
    name: str
    split_counts: NotRequired[dict[str, int] | None]


class SnapshotLineageSnapshot(TypedDict):
    """
    The snapshot side of a lineage answer.
    """

    created_at: str
    digest: str
    id: str
    item_count: int
    name: str


class SnapshotLineageVersion(TypedDict):
    """
    A model version trained on the snapshot, with its downstream footprint.
    """

    created_at: str
    id: str
    items_predicted: int
    model_id: str
    model_name: str
    snapshot_digest: str | None
    training_run: dict[str, Any] | None
    version: int


class SnapshotPublishRequest(TypedDict):
    """
    `POST /projects/{id}/snapshots/{sid}/mlflow`.
    """

    experiment: NotRequired[str | None]
    ml_platform_id: str


class SnapshotPublishResult(TypedDict):
    """
    The MLflow run that stands for the snapshot.
    """

    created: bool
    experiment_id: str
    experiment_name: str
    ml_platform_id: str
    run_id: str
    run_url: str | None


class SnapshotRead(TypedDict):
    """
    A frozen, immutable dataset (EXP-1) as returned by the API.
    """

    blob_path: str
    created_at: str
    created_by_id: str
    digest: str
    filter: dict[str, Any]
    id: str
    item_count: int
    label_schema_version_id: str
    name: str
    project_id: str
    split: NotRequired[dict[str, Any] | None]


class SpanAgreement(TypedDict):
    """
    Pooled span agreement: exact and overlap F1 (QA-2).
    """

    f1_exact: float | None
    f1_overlap: float | None


type Box = tuple[float, float, float, float]


SpanShape = TypedDict(
    "SpanShape",
    {
        "attributes": NotRequired[dict[str, Any]],
        "boxes": NotRequired[list[Box] | None],
        "class": str,
        "confidence": NotRequired[float | None],
        "end": NotRequired[int | None],
        "frame": NotRequired[int | None],
        "id": str,
        "keyframe": NotRequired[bool],
        "outside": NotRequired[bool],
        "page": NotRequired[int | None],
        "start": NotRequired[int | None],
        "text": NotRequired[str | None],
        "track_id": NotRequired[str | None],
        "type": NotRequired[Literal["span"]],
    },
)


class SplitGrid(TypedDict):
    """
    Split an image into `rows` x `cols` equal cells, 1-16 each.

    Cells extend by `overlap_px` on their *inner* edges only (an edge cell's
    outer border stays at the image edge), then are clipped to the image.
    """

    cols: int
    overlap_px: NotRequired[float]
    rows: int


type Region = tuple[float, float, float, float]


class SplitRequest(TypedDict):
    """
    Body of `POST /items/{id}/split`: exactly one of `grid` or `regions`.
    """

    grid: NotRequired[SplitGrid | None]
    regions: NotRequired[list[Region] | None]


type TaskStatus = Literal["open", "in_progress", "done", "cancelled"]


type TaskType = Literal["annotate", "review"]


class TaskTypeStats(TypedDict):
    cancelled: NotRequired[int]
    done: NotRequired[int]
    in_progress: NotRequired[int]
    open: NotRequired[int]


class TaskUpdate(TypedDict):
    """
    Partial update for a live task (WF-6): only the fields present in the body change.

    `deadline: null` / `assignee_id: null` clear the field; leaving a key out
    leaves it alone (see `model_fields_set`). Status and lock fields are not
    settable here — they move through claim / release / complete only.
    """

    assignee_id: NotRequired[str | None]
    deadline: NotRequired[str | None]
    priority: NotRequired[int | None]


class TelemetryPreview(TypedDict):
    """
    Exactly what the heartbeat would send, plus what was withheld and what was sent.
    """

    active_users: int
    attempted_at: NotRequired[str | None]
    enabled: bool
    error: NotRequired[str | None]
    fingerprint: dict[str, Any]
    install_id: str | None
    last_payload: NotRequired[dict[str, Any] | None]
    licence_type: str
    notice: NotRequired[str | None]
    sent_at: NotRequired[str | None]
    server_configured: NotRequired[bool]
    version: str
    withheld: NotRequired[list[str]]


class ThroughputDay(TypedDict):
    approved: NotRequired[int]
    day: str
    rejected: NotRequired[int]
    submitted: NotRequired[int]


class ThumbnailRequest(TypedDict):
    """
    Body for `POST /projects/{id}/thumbnails` (IMG-8).
    """

    force: NotRequired[bool]
    item_ids: NotRequired[list[str] | None]


class TileJobRequest(TypedDict):
    """
    Body for `POST /projects/{id}/tiles` (IMG-1).
    """

    force: NotRequired[bool]
    item_ids: NotRequired[list[str] | None]


type Tile = tuple[int, int, int]


class TileSignRequest(TypedDict):
    """
    Body for `POST /items/{id}/tiles/sign`: `[level, col, row]` triples.
    """

    tiles: list[Tile]


class TileSignResponse(TypedDict):
    """
    Signed URLs for the requested tiles, in the same order as the request.
    """

    expires_in: int
    urls: list[str]


class TokenResponse(TypedDict):
    """
    Access token issued on successful login.
    """

    access_token: str
    expires_in: int
    mfa_setup_required: NotRequired[bool]
    token_type: NotRequired[str]


type ToolType = Literal[
    "bbox",
    "rbox",
    "polygon",
    "polyline",
    "point",
    "mask",
    "keypoints",
    "span",
    "relation",
    "classification",
    "ranking",
    "rating",
    "segment",
]


class UnreadCount(TypedDict):
    """
    `GET /notifications/unread-count`.
    """

    count: int


class UploadFileSpec(TypedDict):
    """
    One file the browser wants to upload. `path` is relative to the project's source prefix.
    """

    content_type: NotRequired[str | None]
    path: str
    size_bytes: NotRequired[int | None]


class UploadTarget(TypedDict):
    """
    Where and how to PUT one file. `path` is the object path the item will be registered at.
    """

    headers: NotRequired[dict[str, str]]
    method: NotRequired[str]
    path: str
    url: str


class UploadUrlsRequest(TypedDict):
    """
    Body for `POST /projects/{id}/uploads`.
    """

    files: list[UploadFileSpec]


class UploadUrlsResponse(TypedDict):
    """
    One write-scoped signed URL per requested file.
    """

    prefix: str
    uploads: list[UploadTarget]


class UsageNoticeOut(TypedDict):
    """
    One sign of seat sharing (LIC-31). A notice for the admin, never a block.
    """

    count: int
    detail: str
    display_name: str
    email: str
    kind: str
    user_id: str


class UsageNotices(TypedDict):
    """
    `GET /license/usage-notices`.
    """

    notices: list[UsageNoticeOut]
    window_days: int


class UserPreferencesUpdate(TypedDict):
    """
    `PATCH /auth/me`: the caller's own preferences; only keys present change.
    """

    email_notifications: NotRequired[bool | None]


class UserRead(TypedDict):
    """
    User as returned by the API. `password_hash` is never exposed.
    """

    created_at: str
    display_name: str
    email: str
    email_notifications: NotRequired[bool]
    erased_at: NotRequired[str | None]
    id: str
    idp_subject: str | None
    is_active: bool
    is_service: NotRequired[bool]
    is_superuser: bool
    last_seen_at: str | None
    mfa_enabled: NotRequired[bool]
    organization_id: str
    updated_at: str


class ValidationError(TypedDict):
    ctx: NotRequired[dict[str, Any]]
    input: NotRequired[Any]
    loc: list[str | int]
    msg: str
    type: str


type WebhookDeliveryStatus = Literal["pending", "succeeded", "failed"]


type WebhookFormat = Literal["json", "slack", "teams"]


class WebhookRead(TypedDict):
    """
    A webhook as returned by the API; the secret is never included.
    """

    created_at: str
    created_by_id: str | None
    description: str | None
    events: list[str]
    format: NotRequired[WebhookFormat]
    id: str
    is_active: bool
    last_delivery_at: str | None
    last_response_status: int | None
    organization_id: str
    project_id: str | None
    updated_at: str
    url: str


class WebhookUpdate(TypedDict):
    """
    `PATCH /webhooks/{id}`: only keys present change. `rotate_secret: true`
    replaces the signing secret and returns the new one once.
    """

    description: NotRequired[str | None]
    events: NotRequired[list[str] | None]
    format: NotRequired[WebhookFormat | None]
    is_active: NotRequired[bool | None]
    rotate_secret: NotRequired[bool]
    url: NotRequired[str | None]


class WebhookUpdated(TypedDict):
    """
    `PATCH` response: `secret` is set only when it was rotated in this call.
    """

    created_at: str
    created_by_id: str | None
    description: str | None
    events: list[str]
    format: NotRequired[WebhookFormat]
    id: str
    is_active: bool
    last_delivery_at: str | None
    last_response_status: int | None
    organization_id: str
    project_id: str | None
    secret: NotRequired[str | None]
    updated_at: str
    url: str


class WorkflowConfig(TypedDict):
    """
    `project.workflow`, see CONTRACTS.md *Project workflow JSON* (WF-1).

    Every field defaults to the standard annotate → review → approve flow, so
    `{}` and pre-existing rows behave as before. Unknown keys are refused so a
    typo cannot silently leave the default in force.
    """

    allow_self_review: NotRequired[bool]
    allow_skip: NotRequired[bool]
    consensus_annotators: NotRequired[int]
    gold_every: NotRequired[int | None]
    rejection_returns_to: NotRequired[RejectionTarget]
    review: NotRequired[ReviewMode]
    review_sample_rate: NotRequired[float]


type AppModelsItemItemStatus = Literal[
    "new",
    "prelabeled",
    "annotating",
    "submitted",
    "in_review",
    "approved",
    "rejected",
    "skipped",
]


type AppSchemasItemItemStatus = Literal[
    "new",
    "prelabeled",
    "annotating",
    "submitted",
    "in_review",
    "approved",
    "rejected",
    "skipped",
]


type Shapes = (
    BBoxShape
    | RBoxShape
    | PolygonShape
    | PolylineShape
    | PointShape
    | MaskShape
    | KeypointsShape
    | SpanShape
    | RelationShape
    | RankingShape
    | RatingShape
    | SegmentShape
)


class AnnotationResult(TypedDict):
    """
    Top-level annotation result stored in `annotation.result` (DATA-8).
    """

    classification: NotRequired[dict[str, Any]]
    media_type: MediaType
    schema_version: int
    shapes: NotRequired[list[Shapes]]


class AttributeDef(TypedDict):
    """
    One attribute definition, attached to a class or to top-level classification.
    """

    default: NotRequired[Any]
    name: str
    options: NotRequired[list[str] | None]
    required: NotRequired[bool]
    type: AttributeType


class AuthProviders(TypedDict):
    """
    Which sign-in methods this installation offers.
    """

    local: NotRequired[bool]
    oidc: NotRequired[OidcProviderInfo | None]


class BodyUploadImportJobApiV1ProjectsProjectIdImportsUploadPost(TypedDict):
    attribute_mapping: NotRequired[str | None]
    class_mapping: NotRequired[str | None]
    dry_run: NotRequired[bool]
    file: str
    format: str
    label_schema_version_id: NotRequired[str | None]
    status: NotRequired[ImportStatus]


class BulkAssign(TypedDict):
    """
    Point the items' live `type` tasks at `assignee_id` (or nobody), and /
    or set their priority and deadline. An item without a live task of that
    type gets one opened when its status allows annotation (`annotate`) or
    review (`review`); items being worked on right now (`in_progress`) are
    skipped rather than pulled from under the annotator.
    """

    action: Literal["assign"]
    assignee_id: NotRequired[str | None]
    deadline: NotRequired[str | None]
    item_ids: list[str]
    priority: NotRequired[int | None]
    type: NotRequired[TaskType]


class BulkResult(TypedDict):
    """
    Outcome of a bulk request: how many items changed, and which did not.
    """

    applied: int
    skipped: list[BulkSkipped]


class ClaimRequest(TypedDict):
    """
    Optional JSON body for `POST /tasks/next`, alternative to the query parameter.
    """

    project_id: NotRequired[str | None]
    type: NotRequired[TaskType | None]


class ClassDef(TypedDict):
    """
    One annotation class: its display, tools and attributes.
    """

    attributes: NotRequired[list[AttributeDef]]
    color: str
    display_name: str
    hotkey: NotRequired[str | None]
    name: str
    scale: NotRequired[ScaleDef | None]
    skeleton: NotRequired[SkeletonDef | None]
    tools: list[ToolType]


class ConnectorCreate(TypedDict):
    """
    Payload to register a new storage connector.

    `secret_ref` is a Key Vault / secret-store reference, never a raw secret
    (SEC/AUTH-7).
    """

    config: NotRequired[dict[str, Any]]
    identity_type: ConnectorIdentity
    name: str
    secret_ref: NotRequired[str | None]
    type: ConnectorType


class ConnectorRead(TypedDict):
    """
    Connector as returned by the API; `secret_ref` is never exposed, only its presence.
    """

    config: dict[str, Any]
    created_at: str
    events_enabled: NotRequired[bool]
    has_secret: bool
    id: str
    identity_type: ConnectorIdentity
    name: str
    organization_id: str
    type: ConnectorType
    updated_at: str


class CorrectionMetrics(TypedDict):
    """
    `GET /models/{id}/versions/{vid}/metrics` (ML-5).
    """

    classes: list[CorrectionClassMetrics]
    items_accepted_unchanged: int
    items_corrected: int
    items_pending: int
    items_predicted: int
    mean_iou_adjusted: float | None
    model_version_id: str
    precision: float | None
    project_id: str | None
    recall: float | None
    shapes: CorrectionShapeCounts


class DatasetFilter(TypedDict):
    """
    Which items a snapshot or export covers (EXP-2). Empty means every annotated item.

    Every field is a further restriction on the *latest* annotation version
    of each item; the field list is documented in CONTRACTS.md → snapshot.
    """

    annotated_after: NotRequired[str | None]
    annotated_before: NotRequired[str | None]
    annotation_status: NotRequired[list[AnnotationStatus] | None]
    annotator_ids: NotRequired[list[str] | None]
    classes: NotRequired[list[str] | None]
    item_status: NotRequired[list[AppModelsItemItemStatus] | None]
    path_prefix: NotRequired[str | None]
    source: NotRequired[list[AnnotationSource] | None]


class ExportRequest(TypedDict):
    """
    Body for queuing an export job (EXP-5).

    Either `snapshot_id` (export exactly that frozen set) or `filter` (export
    the latest annotation of every matching item right now). `split` picks
    one partition of a split snapshot (EXP-3); it needs `snapshot_id`.
    """

    filter: NotRequired[DatasetFilter]
    format: str
    label_schema_version_id: NotRequired[str | None]
    snapshot_id: NotRequired[str | None]
    split: NotRequired[Literal["train", "val", "test"] | None]


class HTTPValidationError(TypedDict):
    detail: NotRequired[list[ValidationError]]


class ImportRequest(TypedDict):
    """
    Body for queuing an import job (EXP-6).

    `path` names the file, `.zip` archive or `/`-terminated prefix on
    `connector_id` (default: the project's source connector). `dry_run`
    parses and matches without writing anything — the preview to run first.
    """

    attribute_mapping: NotRequired[dict[str, dict[str, str | None]]]
    class_mapping: NotRequired[dict[str, str]]
    connector_id: NotRequired[str | None]
    dry_run: NotRequired[bool]
    format: str
    label_schema_version_id: NotRequired[str | None]
    path: str
    status: NotRequired[ImportStatus]


class ItemAgreement(TypedDict):
    """
    Agreement over one item's consensus versions (embedded in `GET /items/{id}/consensus`).
    """

    classification: NotRequired[list[ClassificationAgreement]]
    pairs: NotRequired[list[PairAgreement]]
    shapes: ShapeAgreement
    spans: SpanAgreement


class ItemCreate(TypedDict):
    """
    Payload to register a new item under a project (project id comes from the route).
    """

    connector_id: str
    etag: NotRequired[str | None]
    height: NotRequired[int | None]
    media_type: MediaType
    meta: NotRequired[dict[str, Any]]
    path: str
    size_bytes: int
    width: NotRequired[int | None]


class ItemRead(TypedDict):
    """
    Item as returned by the API, with an optional short-lived signed media URL.
    """

    connector_id: str
    created_at: str
    etag: str | None
    height: int | None
    id: str
    media_type: MediaType
    media_url: NotRequired[str | None]
    meta: dict[str, Any]
    path: str
    project_id: str
    size_bytes: int
    status: ItemStatusOutput
    thumbnail_url: NotRequired[str | None]
    updated_at: str
    width: int | None


class ItemView(TypedDict):
    """
    `GET /items/{id}/views`: one companion view, signed (§5 multimodal).
    """

    label: NotRequired[str | None]
    media_type: NotRequired[MediaType | None]
    path: str
    url: NotRequired[str | None]


class JobRead(TypedDict):
    """
    Job as returned by the API, e.g. `GET /jobs/{id}` for status and progress.
    """

    attempts: int
    created_at: str
    error: str | None
    finished_at: str | None
    id: str
    payload: dict[str, Any]
    progress: int
    project_id: str | None
    result: dict[str, Any] | None
    started_at: str | None
    status: JobStatusOutput
    type: JobTypeOutput
    updated_at: str


class LabelSchemaDefinition(TypedDict):
    """
    The full label schema stored in `label_schema_version.definition`.
    """

    classes: list[ClassDef]
    classification: NotRequired[list[AttributeDef]]
    version: int


class MemberCreate(TypedDict):
    """
    Payload to add a member to a project — owner only.

    Exactly one of `user_id` / `email` identifies the user to add; the other
    must be omitted.
    """

    email: NotRequired[str | None]
    path_prefixes: NotRequired[list[str] | None]
    role: ProjectRole
    user_id: NotRequired[str | None]


class MemberRead(TypedDict):
    """
    A project member, joined with the `user` row for display fields.
    """

    created_at: str
    display_name: str
    email: str
    path_prefixes: NotRequired[list[str] | None]
    role: ProjectRole
    source: NotRequired[Literal["manual", "idp"]]
    user_id: str


class MemberUpdate(TypedDict):
    """
    Payload to change a member's role and / or folders — owner only; keys present change.
    """

    path_prefixes: NotRequired[list[str] | None]
    role: NotRequired[ProjectRole | None]


class MlPlatformCreate(TypedDict):
    """
    Register a platform. `secret_ref` is a secret-store reference (AUTH-7).
    """

    config: NotRequired[dict[str, Any]]
    identity_type: MlIdentity
    kind: MlPlatformKind
    name: str
    secret_ref: NotRequired[str | None]
    tracking_uri: str


class ModelCreate(TypedDict):
    """
    Payload to register a new model endpoint.

    `secret_ref` is a secret-store reference, never a raw credential
    (SEC/AUTH-7): the key or token for `api_key` / `bearer`, the client
    secret for `service_principal`, and absent for `none` and
    `managed_identity`. Entra identities also need `identity_config`.
    """

    endpoint_url: NotRequired[str | None]
    identity_config: NotRequired[ModelIdentityConfig]
    identity_type: NotRequired[ModelIdentity]
    name: str
    secret_ref: NotRequired[str | None]
    task: ModelTask


class ModelFamilyVersion(TypedDict):
    """
    A version in a derivation graph, with the model it belongs to (EXP-8).
    """

    class_mapping: dict[str, str | None]
    created_at: str
    derivation: NotRequired[ModelDerivation | None]
    id: str
    metrics: dict[str, Any]
    model_id: str
    model_name: str
    model_task: ModelTask
    parent_version_id: NotRequired[str | None]
    snapshot_digest: NotRequired[str | None]
    snapshot_id: NotRequired[str | None]
    training_run: NotRequired[dict[str, Any] | None]
    version: int


class ModelRead(TypedDict):
    """
    Model as returned by the API; `secret_ref` is never exposed, only its presence.
    """

    created_at: str
    endpoint_url: str | None
    has_secret: bool
    id: str
    identity_config: ModelIdentityConfig
    identity_type: ModelIdentity
    name: str
    organization_id: str
    task: ModelTask


class NotificationRead(TypedDict):
    """
    One notification as returned by the API.
    """

    created_at: str
    id: str
    payload: dict[str, Any]
    read_at: str | None
    type: NotificationType
    user_id: str


class OcrResult(TypedDict):
    """
    The words an `ocr` model read on a page. Nothing is stored.
    """

    engine: str
    height: float
    page: int
    width: float
    words: list[OcrWord]


class PageConnectorRead(TypedDict):
    items: list[ConnectorRead]
    next_cursor: NotRequired[str | None]


class PageItemRead(TypedDict):
    items: list[ItemRead]
    next_cursor: NotRequired[str | None]


class PageJobRead(TypedDict):
    items: list[JobRead]
    next_cursor: NotRequired[str | None]


class PageModelRead(TypedDict):
    items: list[ModelRead]
    next_cursor: NotRequired[str | None]


class PageNotificationRead(TypedDict):
    items: list[NotificationRead]
    next_cursor: NotRequired[str | None]


class PageSnapshotRead(TypedDict):
    items: list[SnapshotRead]
    next_cursor: NotRequired[str | None]


class PageWebhookRead(TypedDict):
    items: list[WebhookRead]
    next_cursor: NotRequired[str | None]


class PersonalDataExport(TypedDict):
    """
    Everything the platform holds about one person (SEC-6 access).
    """

    annotations: NotRequired[list[PersonalAnnotation]]
    api_keys: NotRequired[list[PersonalApiKey]]
    audit_events: NotRequired[list[PersonalAuditEvent]]
    comments: NotRequired[list[PersonalComment]]
    generated_at: str
    memberships: NotRequired[list[PersonalMembership]]
    notifications: NotRequired[list[PersonalNotification]]
    tasks: NotRequired[list[PersonalTask]]
    user: PersonalProfile


class PrelabelCreate(TypedDict):
    """
    An external producer's pre-label (API-8): a model-authored draft.
    """

    label_schema_version_id: NotRequired[str | None]
    model_version_id: str
    result: AnnotationResult


class PrelabelFilter(TypedDict):
    """
    Which items a pre-labelling job covers (ML-2).

    Defaults to the items a customer would normally want touched: unstarted
    and previously pre-labelled ones. Items with a human annotation version
    are never selected, whichever statuses are listed here (ML-10).
    """

    item_status: NotRequired[list[AppSchemasItemItemStatus]]
    path_prefix: NotRequired[str | None]


class PrelabelRequest(TypedDict):
    """
    Body for queuing a pre-labelling job (ML-2).

    `limit` caps how many items are sent to the model in this run — a dry
    run over a handful of items before committing to the whole project
    (BYOM-7).
    """

    confidence_threshold: NotRequired[float]
    filter: NotRequired[PrelabelFilter]
    label_schema_version_id: NotRequired[str | None]
    limit: NotRequired[int | None]
    model_version_id: str
    prioritize_uncertain: NotRequired[bool]


class ProjectAgreement(TypedDict):
    """
    Project-wide inter-annotator agreement (`GET /projects/{id}/agreement`, QA-2).
    """

    annotators: NotRequired[list[AgreementAnnotator]]
    classification: NotRequired[list[ClassificationAgreement]]
    items: int
    pairs: NotRequired[list[PairAgreement]]
    shapes: ShapeAgreement
    spans: SpanAgreement


class ProjectCreate(TypedDict):
    """
    Payload to create a new project (organization id comes from the auth context).
    """

    cache_connector_id: NotRequired[str | None]
    description: NotRequired[str | None]
    label_schema_id: NotRequired[str | None]
    name: str
    result_connector_id: NotRequired[str | None]
    settings: NotRequired[dict[str, Any]]
    source_connector_id: NotRequired[str | None]
    source_glob: NotRequired[str | None]
    source_prefix: NotRequired[str | None]
    workflow: NotRequired[WorkflowConfig]


class ProjectRead(TypedDict):
    """
    Project as returned by the API.
    """

    cache_connector_id: str | None
    created_at: str
    description: str | None
    id: str
    label_schema_id: str | None
    name: str
    organization_id: str
    result_connector_id: str | None
    settings: dict[str, Any]
    source_connector_id: str | None
    source_glob: str | None
    source_prefix: str | None
    updated_at: str
    workflow: WorkflowConfig


class ProjectUpdate(TypedDict):
    """
    Partial update payload for a project; all fields optional.
    """

    cache_connector_id: NotRequired[str | None]
    description: NotRequired[str | None]
    label_schema_id: NotRequired[str | None]
    name: NotRequired[str | None]
    result_connector_id: NotRequired[str | None]
    settings: NotRequired[dict[str, Any] | None]
    source_connector_id: NotRequired[str | None]
    source_glob: NotRequired[str | None]
    source_prefix: NotRequired[str | None]
    workflow: NotRequired[WorkflowConfig | None]


class ReviewRequest(TypedDict):
    """
    A reviewer's verdict on a submitted annotation (WF-4).
    """

    approve: bool
    comment: NotRequired[str | None]
    corrected_result: NotRequired[AnnotationResult | None]


class SeatReport(TypedDict):
    """
    `GET /license/seat-report`: distinct active users per month for true-up (LIC-30).

    Overage is measured against the licence in force now. Not signed; the
    licence terms back it.
    """

    end: str
    generated_at: str
    install_id: str | None
    license_id: str | None
    licensee: str | None
    peak_active_users: int
    peak_overage: int | None
    periods: list[SeatReportPeriod]
    seat_limit: int | None
    seats: int | None
    start: str
    tier: str


class SnapshotDiff(TypedDict):
    """
    `GET /projects/{id}/snapshots/{base}/diff/{target}` (EXP-4).
    """

    added: list[SnapshotDiffEntry]
    base: SnapshotDiffSide
    changed: list[SnapshotDiffChanged]
    classes: list[SnapshotDiffClass]
    items: SnapshotDiffItems
    removed: list[SnapshotDiffEntry]
    target: SnapshotDiffSide
    truncated: bool


class SnapshotLineage(TypedDict):
    """
    `GET /projects/{id}/snapshots/{sid}/lineage` (EXP-8).
    """

    snapshot: SnapshotLineageSnapshot
    versions: list[SnapshotLineageVersion]


class TaskCreate(TypedDict):
    """
    Payload to create/assign a task for an item (project id comes from the route).
    """

    assignee_id: NotRequired[str | None]
    deadline: NotRequired[str | None]
    item_id: str
    priority: NotRequired[int]
    slot: NotRequired[int | None]
    type: TaskType


class TaskRead(TypedDict):
    """
    Task as returned by the API.
    """

    assignee_id: str | None
    created_at: str
    deadline: str | None
    gold: NotRequired[bool]
    id: str
    item_id: str
    locked_by_id: str | None
    locked_until: str | None
    priority: int
    project_id: str
    region: NotRequired[tuple[float, float, float, float] | None]
    slot: NotRequired[int | None]
    status: TaskStatus
    type: TaskType
    updated_at: str


class TaskStats(TypedDict):
    annotate: TaskTypeStats
    review: TaskTypeStats


class WebhookCreate(TypedDict):
    """
    `POST /webhooks`: subscribe a URL to events.

    `project_id` null subscribes to every project in the organisation
    (superuser only); set, the caller must own that project. `events` names
    the events to receive, or `["*"]` for all of them.
    """

    description: NotRequired[str | None]
    events: list[str]
    format: NotRequired[WebhookFormat]
    is_active: NotRequired[bool]
    project_id: NotRequired[str | None]
    url: str


class WebhookCreated(TypedDict):
    """
    The freshly created (or rotated) webhook, carrying `secret` exactly once.
    """

    created_at: str
    created_by_id: str | None
    description: str | None
    events: list[str]
    format: NotRequired[WebhookFormat]
    id: str
    is_active: bool
    last_delivery_at: str | None
    last_response_status: int | None
    organization_id: str
    project_id: str | None
    secret: str
    updated_at: str
    url: str


class WebhookDeliveryRead(TypedDict):
    """
    One delivery, for the per-hook delivery log.
    """

    attempts: int
    created_at: str
    delivered_at: str | None
    error: str | None
    event: str
    id: str
    next_attempt_at: str
    payload: dict[str, Any]
    response_status: int | None
    status: WebhookDeliveryStatus
    webhook_id: str


class AnnotationCreate(TypedDict):
    """
    A new annotation version.
    """

    duration_ms: NotRequired[int | None]
    label_schema_version_id: str
    result: AnnotationResult
    submit: NotRequired[bool]
    task_id: NotRequired[str | None]


class ConsensusRead(TypedDict):
    """
    `GET /items/{id}/consensus` response body (QA-1, QA-2, QA-3).
    """

    agreement: ItemAgreement
    annotators: list[ConsensusAnnotatorRead]
    conflicts: list[str]
    expected: int
    preview: AnnotationResult


class ModelFamily(TypedDict):
    """
    Every version connected to a model's versions through parent links.
    """

    versions: list[ModelFamilyVersion]


class PageProjectRead(TypedDict):
    items: list[ProjectRead]
    next_cursor: NotRequired[str | None]


class PageTaskRead(TypedDict):
    items: list[TaskRead]
    next_cursor: NotRequired[str | None]


class PageWebhookDeliveryRead(TypedDict):
    items: list[WebhookDeliveryRead]
    next_cursor: NotRequired[str | None]


class ProjectStats(TypedDict):
    annotations: AnnotationStats
    annotators: list[AnnotatorStats]
    classes: list[ClassCount]
    items: ItemStats
    review: ReviewStats
    tasks: TaskStats
    throughput: list[ThroughputDay]


class SplitResponse(TypedDict):
    """
    One region task per region opened by the split.
    """

    tasks: list[TaskRead]
