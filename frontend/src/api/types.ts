/**
 * Hand-written TypeScript types mirroring the DTOs described in
 * docs/CONTRACTS.md ("Label schema JSON", "Annotation result JSON",
 * "Data model", and the REST API table). Keep these in sync with the
 * backend Pydantic schemas — this file is the single source of truth for
 * the frontend's view of the wire format.
 */

// ---------------------------------------------------------------------------
// Enums (string-literal unions)
// ---------------------------------------------------------------------------

export type MediaType = 'image' | 'video' | 'audio' | 'text' | 'pdf' | 'llm' | 'timeseries'

/** `GET /items/{id}/views`: a companion view, signed (§5 multimodal). */
export interface ItemView {
  path: string
  label: string | null
  media_type: MediaType | null
  url: string | null
}

export type ItemStatus =
  | 'new'
  | 'prelabeled'
  | 'annotating'
  | 'submitted'
  | 'in_review'
  | 'approved'
  | 'rejected'
  | 'skipped'

export type TaskType = 'annotate' | 'review'

export type TaskStatus = 'open' | 'in_progress' | 'done' | 'cancelled'

export type AnnotationSource = 'human' | 'model'

export type AnnotationStatus = 'draft' | 'submitted' | 'approved' | 'rejected'

export type AnnotationKind = 'primary' | 'consensus' | 'gold'

export type JobType =
  | 'scan_source'
  | 'tile_image'
  | 'prelabel'
  | 'export'
  | 'snapshot'
  | 'import'
  | 'thumbnail'
  | 'rebuild_cache'

export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'

export type ProjectRole = 'owner' | 'annotator' | 'reviewer' | 'viewer'

export type ConnectorType =
  | 'azure_blob'
  | 's3'
  | 'gcs'
  | 'local'
  | 'http'
  | 'sharepoint'
  | 'databricks_volume'

export type ConnectorIdentity =
  | 'managed_identity'
  | 'service_principal'
  | 'account_key'
  | 'sas_token'
  | 'iam_role'
  | 'access_key'
  | 'none'

export type ModelTask = 'detect' | 'segment' | 'classify' | 'ner' | 'llm' | 'ocr'

export type ModelIdentity =
  | 'none'
  | 'api_key'
  | 'bearer'
  | 'service_principal'
  | 'managed_identity'

/**
 * The non-secret half of an Entra identity (BYOM-3). `scope` is the token
 * audience (`https://ml.azure.com/.default`); `tenant_id` + `client_id` name a
 * service principal; for a managed identity `client_id` picks a user-assigned one.
 */
export interface ModelIdentityConfig {
  scope?: string | null
  tenant_id?: string | null
  client_id?: string | null
}

// ---------------------------------------------------------------------------
// Label schema JSON (TOOL-1, TOOL-2)
// ---------------------------------------------------------------------------

export type AttributeType = 'text' | 'number' | 'select' | 'multiselect' | 'boolean'

export type ShapeTool =
  | 'bbox'
  | 'rbox'
  | 'polygon'
  | 'polyline'
  | 'point'
  | 'mask'
  | 'keypoints'
  | 'span'
  | 'relation'
  | 'classification'
  | 'ranking'
  | 'segment'
  | 'rating'

export interface LabelAttribute {
  name: string
  type: AttributeType
  required: boolean
  default?: string | number | boolean | null
  options?: string[]
}

export interface LabelClass {
  name: string
  display_name: string
  color: string
  hotkey?: string
  tools: ShapeTool[]
  attributes: LabelAttribute[]
  /** Required with the `keypoints` tool, absent otherwise. */
  skeleton?: Skeleton | null
  /** Required with the `rating` tool, absent otherwise (`llm` items). */
  scale?: Scale | null
}

/** A `keypoints` class's named points (in shape order) and its 0-based bones. */
export interface Skeleton {
  points: string[]
  edges: [number, number][]
}

/** A `rating` class's integer scale: `min < max`, at most 21 steps;
 * `labels` (optional) names some or all of the values, keyed by the integer
 * as a string. */
export interface Scale {
  min: number
  max: number
  labels?: Record<string, string>
}

export interface ClassificationField {
  name: string
  type: AttributeType
  required: boolean
  options?: string[]
}

export interface LabelSchemaDefinition {
  version: number
  classes: LabelClass[]
  classification: ClassificationField[]
}

export interface LabelSchemaVersion {
  id: string
  label_schema_id: string
  version: number
  definition: LabelSchemaDefinition
  created_at: string
}

// ---------------------------------------------------------------------------
// Annotation result JSON (DATA-8)
// ---------------------------------------------------------------------------

/** [xMin, yMin, xMax, yMax] in original-image pixel coordinates. */
export type BBox = [number, number, number, number]

/** A single [x, y] pixel coordinate pair. */
export type Point2D = [number, number]

/** One attribute value; `string[]` only for `multiselect` (TOOL-2). */
export type AttributeValue = string | number | boolean | string[] | null

export interface ShapeAttributes {
  [key: string]: AttributeValue
}

interface ShapeBase {
  id: string
  class: string
  attributes: ShapeAttributes
  /** null for human annotations, 0.0-1.0 for model output (ML-3). */
  confidence: number | null
  /** Video only: 0-based frame, required on video items, absent elsewhere. */
  frame?: number | null
  /** Video only: shared by the keyframes of one object track. */
  track_id?: string | null
  keyframe?: boolean
  /** Video only: the object has left the view from this frame on. */
  outside?: boolean
  /** PDF only: 1-based page; coordinates are that page's points (CONTRACTS "PDF items"). */
  page?: number | null
}

export interface BBoxShape extends ShapeBase {
  type: 'bbox'
  bbox: BBox
  /** Informational: the document text inside the box (PDF annotator). */
  text?: string | null
}

export interface RBoxShape extends ShapeBase {
  type: 'rbox'
  /** [cx, cy] in pixels. */
  center: Point2D
  /** [w, h], both > 0; w runs along the rotated x axis. */
  size: [number, number]
  /** Degrees, clockwise-positive (y points down), in (-180, 180]. */
  angle: number
}

export interface PolygonShape extends ShapeBase {
  type: 'polygon'
  /** Flat list of [x, y] pairs, not closed. */
  points: Point2D[]
}

export interface PolylineShape extends ShapeBase {
  type: 'polyline'
  points: Point2D[]
}

export interface PointShape extends ShapeBase {
  type: 'point'
  point: Point2D
}

/** Uncompressed COCO RLE: `size` is `[height, width]`, `counts` alternate
 * background / foreground runs in column-major order, starting with background,
 * summing to `height × width`. */
export interface MaskRLE {
  size: [number, number]
  counts: number[]
}

/** COCO visibility: 0 not labelled (x, y are 0), 1 occluded, 2 visible. */
export type KeypointVisibility = 0 | 1 | 2
export type Keypoint = [number, number, KeypointVisibility]

/** One skeleton instance: a `[x, y, v]` per point of the class's skeleton. */
export interface KeypointsShape extends ShapeBase {
  type: 'keypoints'
  points: Keypoint[]
}

export interface MaskShape extends ShapeBase {
  type: 'mask'
  rle: MaskRLE
}

/**
 * Text span (TOOL): `[start, end)` in Unicode code points of the item's text —
 * index into `Array.from(text)`, not into the UTF-16 string.
 */
export interface TextSpanShape extends ShapeBase {
  type: 'span'
  start: number
  end: number
  boxes?: null
  /** Covered slice, informational only. */
  text?: string | null
}

/**
 * PDF entity span (CONTRACTS "PDF items"): one box per line on `page`, in PDF
 * points; it covers the words whose centres fall inside the boxes.
 */
export interface PdfSpanShape extends ShapeBase {
  type: 'span'
  page: number
  boxes: BBox[]
  start?: null
  end?: null
  /** The covered words joined by single spaces, informational only. */
  text?: string | null
}

export type SpanShape = TextSpanShape | PdfSpanShape

/** Directed relation between two non-relation shapes of the same result (TOOL). */
export interface RelationShape extends ShapeBase {
  type: 'relation'
  from: string
  to: string
}

/**
 * Best-first ranking of an `llm` item's candidate responses (CONTRACTS "LLM
 * evaluation items"). `order` lists each response id exactly once, at least
 * one; at most one ranking per class.
 */
export interface RankingShape extends ShapeBase {
  type: 'ranking'
  order: string[]
}

/**
 * One integer score of an `llm` item against its class's `scale`. `target`
 * is `response:<id>`, `message:<0-based index>` or `conversation`; at most
 * one rating per (class, target).
 */
export interface RatingShape extends ShapeBase {
  type: 'rating'
  target: string
  value: number
}

/**
 * An interval on an audio or time-series item (§5). Audio: integer
 * milliseconds, optional `speaker` and transcript `text`. Time series:
 * positions on the CSV's time axis, optionally limited to `channels`.
 */
export interface SegmentShape extends ShapeBase {
  type: 'segment'
  start: number
  end: number
  speaker?: string | null
  text?: string | null
  channels?: string[] | null
}

/** Shapes drawn on an image canvas (everything but text spans and relations). */
export type GeometricShape =
  BBoxShape | RBoxShape | PolygonShape | PolylineShape | PointShape | MaskShape | KeypointsShape

/** Discriminated union over the `type` field, per docs/CONTRACTS.md. */
export type Shape =
  | GeometricShape
  | SpanShape
  | RelationShape
  | RankingShape
  | RatingShape
  | SegmentShape

// ---------------------------------------------------------------------------
// LLM evaluation item document (CONTRACTS "LLM evaluation items")
// ---------------------------------------------------------------------------

export type LlmRole = 'system' | 'user' | 'assistant' | 'tool'

export interface LlmMessage {
  role: LlmRole
  content: string
}

export interface LlmResponse {
  id: string
  content: string
  model?: string
}

/**
 * The object an `llm` item's signed `media_url` serves: the conversation so
 * far (`messages`, possibly empty) and the candidate replies to compare
 * (`responses`, possibly empty) — never both empty.
 */
export interface LlmDocument {
  messages: LlmMessage[]
  responses: LlmResponse[]
  meta?: Record<string, unknown>
}

export interface AnnotationResult {
  schema_version: number
  media_type: MediaType
  classification: Record<string, AttributeValue>
  shapes: Shape[]
}

// ---------------------------------------------------------------------------
// Data model entities
// ---------------------------------------------------------------------------

export interface Organization {
  id: string
  name: string
  slug: string
  created_at: string
  updated_at: string
}

export interface User {
  id: string
  organization_id: string
  email: string
  display_name: string
  is_active: boolean
  is_superuser: boolean
  is_service?: boolean
  /** TOTP MFA is on (AUTH-2). */
  mfa_enabled?: boolean
  /** Set once the person was pseudonymised (SEC-6). */
  erased_at?: string | null
  /** E-mail copies of in-app notifications (API-7). */
  email_notifications?: boolean
  last_seen_at: string | null
  created_at: string
  updated_at: string
}

export interface Membership {
  id: string
  user_id: string
  project_id: string
  role: ProjectRole
  created_at: string
}

export interface Connector {
  id: string
  organization_id: string
  name: string
  type: ConnectorType
  identity_type: ConnectorIdentity
  has_secret: boolean
  /** A storage-event token is set (SRC-3); the token itself is never returned. */
  events_enabled: boolean
  config: Record<string, unknown>
  created_at: string
  updated_at: string
}

/** POST /connectors/{id}/events/token — shown once (SRC-3). */
export interface ConnectorEventToken {
  token: string
  /** API path the store posts to, with `?token=` appended. */
  path: string
}

/** GET /scim/token (AUTH-3). */
export interface ScimTokenState {
  enabled: boolean
}

/** POST /scim/token — the token is shown once; `path` is the SCIM base path (AUTH-3). */
export interface ScimToken {
  token: string
  path: string
}

/** POST /connectors — `secret_ref` is a secret-store reference, never a raw secret. */
export interface ConnectorCreate {
  name: string
  type: ConnectorType
  identity_type: ConnectorIdentity
  secret_ref?: string | null
  config?: Record<string, unknown>
}

export type ConnectorUpdate = Partial<ConnectorCreate>

export interface ConnectorCheck {
  ok: boolean
  messages: string[]
}

export interface Model {
  id: string
  organization_id: string
  name: string
  task: ModelTask
  /** Null: an external producer (an AI agent) that posts its own pre-labels (API-8). */
  endpoint_url: string | null
  identity_type: ModelIdentity
  has_secret: boolean
  identity_config: ModelIdentityConfig
  created_at: string
}

/**
 * POST /models — superuser only. `secret_ref` is required for `api_key`,
 * `bearer` and `service_principal`, refused for `managed_identity`; the Entra
 * identities also need `identity_config`.
 */
export interface ModelCreate {
  name: string
  task: ModelTask
  endpoint_url?: string | null
  identity_type?: ModelIdentity
  secret_ref?: string | null
  identity_config?: ModelIdentityConfig | null
}

export type ModelUpdate = Partial<ModelCreate>

export interface ModelCheck {
  ok: boolean
  messages: string[]
  info?: Record<string, unknown> | null
}

/** POST /items/{id}/interactive (ML-7): exactly one of `point` / `box`. */
export interface InteractiveRequest {
  model_id: string
  point?: { x: number; y: number }
  box?: [number, number, number, number]
}

export interface InteractiveResult {
  type: 'polygon'
  points: [number, number][]
  confidence: number
}

/** `POST /items/{id}/ocr`: one page of a scanned PDF, read by an `ocr` model. */
export interface OcrRequest {
  model_id: string
  page: number
}

export interface OcrResult {
  page: number
  width: number
  height: number
  engine: string
  /** `bbox` in the page's points, like pdf shapes. */
  words: Array<{ text: string; bbox: BBox }>
}

export interface ModelVersion {
  id: string
  model_id: string
  version: number
  class_mapping: Record<string, string | null>
  metrics: Record<string, unknown>
  /** Lineage (EXP-8): the snapshot the version was trained on, if recorded. */
  snapshot_id: string | null
  snapshot_digest: string | null
  training_run: Record<string, unknown> | null
  /** Derivation (EXP-8): the version this one was trained, distilled or quantized from. */
  parent_version_id: string | null
  derivation: ModelDerivation | null
  created_at: string
}

export type ModelDerivation = 'trained' | 'distilled' | 'quantized'

/** GET /models/{id}/family — a version with the model it belongs to. */
export interface ModelFamilyVersion extends ModelVersion {
  model_name: string
  model_task: ModelTask
}

export interface ModelFamily {
  versions: ModelFamilyVersion[]
}

/** POST /models/{id}/versions — `version` defaults to the next integer. */
export interface ModelVersionCreate {
  version?: number
  class_mapping?: Record<string, string | null>
  metrics?: Record<string, unknown>
  snapshot_id?: string
  snapshot_digest?: string
  training_run?: Record<string, unknown>
  parent_version_id?: string
  derivation?: ModelDerivation
}

export interface PrelabelRequest {
  model_version_id: string
  label_schema_version_id?: string
  filter?: { item_status?: ItemStatus[]; path_prefix?: string }
  limit?: number
  confidence_threshold?: number
  /** ML-6: order the annotate queue by prediction uncertainty. */
  prioritize_uncertain?: boolean
}

/** `GET /models/{id}/versions/{vid}/metrics` (ML-5). */
export interface CorrectionShapeCounts {
  model: number
  kept: number
  adjusted: number
  relabeled: number
  deleted: number
  added: number
}

export interface CorrectionClassMetrics extends CorrectionShapeCounts {
  name: string
  precision: number | null
  recall: number | null
  mean_iou_adjusted: number | null
}

export interface CorrectionMetrics {
  model_version_id: string
  project_id: string | null
  items_predicted: number
  items_corrected: number
  items_pending: number
  items_accepted_unchanged: number
  shapes: CorrectionShapeCounts
  precision: number | null
  recall: number | null
  mean_iou_adjusted: number | null
  classes: CorrectionClassMetrics[]
}

/** `project.workflow` (WF-1); every key has a server-side default. */
export interface WorkflowConfig {
  /** `none`: submit goes straight to approved, no review task. */
  review: 'required' | 'none' | 'sampled'
  /** Who gets the annotate task a rejection opens. */
  rejection_returns_to: 'same_annotator' | 'queue'
  allow_skip: boolean
  /** May the author of a submission review it themselves. */
  allow_self_review: boolean
  /** N > 1: N independent annotators per item before review (QA-1). */
  consensus_annotators: number
  /** `review: sampled` (QA-7): share of items reviewed, 0 < r ≤ 1. */
  review_sample_rate: number
  /** QA-4: every `gold_every`-th annotate claim is a gold task; null = off. */
  gold_every: number | null
}

export const DEFAULT_WORKFLOW: WorkflowConfig = {
  review: 'required',
  rejection_returns_to: 'same_annotator',
  allow_skip: true,
  allow_self_review: true,
  consensus_annotators: 1,
  review_sample_rate: 0.1,
  gold_every: null,
}

export interface Project {
  id: string
  organization_id: string
  name: string
  description: string | null
  label_schema_id: string | null
  source_connector_id: string | null
  result_connector_id: string | null
  /** Where derived data (`cache/`) lives; null = the result connector (SRC-6). */
  cache_connector_id: string | null
  source_prefix: string | null
  source_glob: string | null
  workflow: WorkflowConfig
  settings: Record<string, unknown>
  created_at: string
  updated_at: string
}

/** POST /projects. */
export interface ProjectCreate {
  name: string
  description?: string | null
  label_schema_id?: string | null
  source_connector_id?: string | null
  result_connector_id?: string | null
  cache_connector_id?: string | null
  source_prefix?: string | null
  source_glob?: string | null
  workflow?: WorkflowConfig
  settings?: Record<string, unknown>
}

/** PATCH /projects/{id} — every field optional. */
export type ProjectUpdate = Partial<ProjectCreate>

/** GET /projects/{id}/members — membership joined with the user row. */
export interface Member {
  user_id: string
  email: string
  display_name: string
  role: ProjectRole
  /** `idp` when SSO group sync granted it (AUTH-3). */
  source?: 'manual' | 'idp'
  /** Folder-level access: only items under these prefixes; null = all (§4). */
  path_prefixes?: string[] | null
  created_at: string
}

/** POST /projects/{id}/members — exactly one of `user_id` / `email`. */
export interface MemberCreate {
  user_id?: string
  email?: string
  role: ProjectRole
  path_prefixes?: string[] | null
}

export interface Item {
  id: string
  project_id: string
  connector_id: string
  path: string
  media_type: MediaType
  etag: string | null
  size_bytes: number
  width: number | null
  height: number | null
  meta: Record<string, unknown>
  status: ItemStatus
  created_at: string
  updated_at: string
  /**
   * Present on GET /items/{id} and on the item list: a short-lived signed URL
   * to the media, or null when the item's connector cannot sign (stub type,
   * bad secret). The grid renders the full object — there are no generated
   * thumbnails yet (IMG-8).
   */
  media_url?: string | null
  /** Signed URL of the generated thumbnail on the result connector; null until the `thumbnail` job ran (IMG-8). */
  thumbnail_url?: string | null
}

export interface Task {
  id: string
  item_id: string
  project_id: string
  type: TaskType
  assignee_id: string | null
  status: TaskStatus
  locked_by_id: string | null
  locked_until: string | null
  priority: number
  deadline: string | null
  /** Consensus replica index (QA-1); null on ordinary tasks. */
  slot: number | null
  /** Image region `[x_min, y_min, x_max, y_max]` (IMG-6); null = whole item. */
  region: BBox | null
  /** Gold task (QA-4): scored against the item's gold reference. */
  gold: boolean
  created_at: string
  updated_at: string
}

/** Body of `POST /projects/{id}/items/bulk` (WF-8), discriminated on `action`. */
export type BulkRequest =
  | {
      action: 'assign'
      item_ids: string[]
      type?: TaskType
      assignee_id?: string | null
      priority?: number
      deadline?: string | null
    }
  | { action: 'return'; item_ids: string[] }
  | { action: 'approve'; item_ids: string[]; comment?: string }
  /** The comment is required: a rejection always says why. */
  | { action: 'reject'; item_ids: string[]; comment: string }
  | { action: 'tag'; item_ids: string[]; add?: string[]; remove?: string[] }

export type BulkAction = BulkRequest['action']

export interface BulkSkipped {
  item_id: string
  reason: string
}

export interface BulkResult {
  applied: number
  skipped: BulkSkipped[]
}

/** Body of `PATCH /tasks/{id}` (WF-6): only keys present change; `null` clears. */
export interface TaskUpdate {
  priority?: number
  deadline?: string | null
  assignee_id?: string | null
}

export interface Annotation {
  id: string
  item_id: string
  task_id: string | null
  version: number
  author_user_id: string | null
  author_model_version_id: string | null
  source: AnnotationSource
  label_schema_version_id: string
  result: AnnotationResult
  status: AnnotationStatus
  duration_ms: number | null
  blob_path: string | null
  /** Only `primary` versions are the item's annotation (QA-1, QA-4). */
  kind: AnnotationKind
  created_at: string
}

export interface Job {
  id: string
  project_id: string | null
  type: JobType
  status: JobStatus
  progress: number
  payload: Record<string, unknown>
  result: Record<string, unknown> | null
  error: string | null
  attempts: number
  started_at: string | null
  finished_at: string | null
  created_at: string
  updated_at: string
}

export type ImportFormat = 'coco' | 'yolo' | 'voc' | 'cvat' | 'label_studio'

/** `POST /projects/{id}/imports`: import a file already on a connector (EXP-6). */
export interface ImportRequest {
  format: ImportFormat
  path: string
  connector_id?: string
  class_mapping?: Record<string, string>
  /**
   * `{schema_class | "*": {source_attribute: schema_attribute | null}}`,
   * looked up by the shape's *mapped* class; a class entry wins over `"*"`
   * key by key, `null` discards the attribute. Identity by default.
   */
  attribute_mapping?: Record<string, Record<string, string | null>>
  status?: 'submitted' | 'draft'
  dry_run?: boolean
  label_schema_version_id?: string
}

export type ImportUploadOptions = Omit<ImportRequest, 'path' | 'connector_id'>

// ---------------------------------------------------------------------------
// Uploads to the source connector (§12 upload path)
// ---------------------------------------------------------------------------

/** One file the browser wants to upload; `path` is relative to the project's source prefix. */
export interface UploadFileSpec {
  path: string
  content_type?: string | null
  size_bytes?: number | null
}

export interface UploadUrlsRequest {
  files: UploadFileSpec[]
}

/** Where to PUT one file. `path` is the object path a scan will register the item at. */
export interface UploadTarget {
  path: string
  url: string
  method: string
  headers: Record<string, string>
}

export interface UploadUrlsResponse {
  prefix: string
  uploads: UploadTarget[]
}

/** `job.result` of a succeeded `import` job (see CONTRACTS `import`). */
export interface ImportResult {
  format: ImportFormat
  dry_run: boolean
  parsed: number
  matched: number
  unmatched: number
  imported: number
  errors: number
  dropped_shapes: number
  /** Attributes dropped because the (mapped) target class does not declare them. */
  dropped_attributes: number
  classes: Record<string, number>
  /** Source attribute names seen per source class, sorted, before mapping. */
  attributes: Record<string, string[]>
  unmatched_sample: string[]
  problems: string[]
}

/** `GET /jobs/{id}/download`: a signed URL for a succeeded export's archive (EXP-5). */
export interface ExportDownload {
  url: string
  /** Seconds the URL stays valid for. */
  expires_in: number
}

export type SplitName = 'train' | 'val' | 'test'

export interface SplitConfig {
  train: number
  val: number
  test: number
  seed: number
  group_by: string | null
}

/** `GET /projects/{id}/snapshots/{base}/diff/{target}` (EXP-4). */
export interface SnapshotDiffSide {
  id: string
  name: string
  item_count: number
  digest: string
  created_at: string
  split_counts: Record<string, number> | null
}

export interface SnapshotDiffEntry {
  item_id: string
  path: string
  version: number
  split: string | null
}

export interface SnapshotDiffChanged {
  item_id: string
  path: string
  from_version: number
  to_version: number
  shapes: { added: number; removed: number; changed: number }
}

export interface SnapshotDiff {
  base: SnapshotDiffSide
  target: SnapshotDiffSide
  items: { added: number; removed: number; changed: number; unchanged: number; split_moved: number }
  added: SnapshotDiffEntry[]
  removed: SnapshotDiffEntry[]
  changed: SnapshotDiffChanged[]
  classes: Array<{ name: string; base: number; target: number; delta: number }>
  truncated: boolean
}

/** GET /projects/{id}/snapshots/{sid}/lineage (EXP-8). */
export interface SnapshotLineageVersion {
  id: string
  model_id: string
  model_name: string
  version: number
  snapshot_digest: string | null
  training_run: Record<string, unknown> | null
  created_at: string
  /** Distinct items of this project the version wrote a draft for. */
  items_predicted: number
}

export interface SnapshotLineage {
  snapshot: { id: string; name: string; digest: string; item_count: number; created_at: string }
  versions: SnapshotLineageVersion[]
}

/** Body of `POST /projects/{id}/snapshots` (EXP-1, EXP-3). */
export interface SnapshotCreate {
  name: string
  filter?: Record<string, unknown>
  label_schema_version_id?: string
  split?: Partial<SplitConfig>
}

export interface Snapshot {
  id: string
  project_id: string
  name: string
  filter: Record<string, unknown>
  /** Train / val / test partition config (EXP-3), null when unsplit. */
  split: SplitConfig | null
  label_schema_version_id: string
  item_count: number
  blob_path: string
  digest: string
  created_by_id: string
  created_at: string
}

export interface Comment {
  id: string
  project_id: string
  item_id: string | null
  annotation_id: string | null
  parent_id: string | null
  author_id: string
  body: string
  anchor: Record<string, unknown> | null
  resolved_at: string | null
  created_at: string
  updated_at: string
}

export interface CommentCreate {
  body: string
  annotation_id?: string
  parent_id?: string
  anchor?: Record<string, unknown>
}

export type NotificationType = 'mention' | 'reply' | 'review'

export interface Notification {
  id: string
  user_id: string
  type: NotificationType
  payload: {
    project_id: string
    item_id: string
    actor_id: string
    comment_id?: string
    excerpt?: string
    annotation_id?: string
    approve?: boolean
    comment?: string | null
  }
  read_at: string | null
  created_at: string
}

// ---------------------------------------------------------------------------
// Envelope / error types
// ---------------------------------------------------------------------------

/** Cursor-paginated list response: `{"items": [...], "next_cursor": "…" | null}`. */
export interface Page<T> {
  items: T[]
  next_cursor: string | null
}

/** RFC 9457 problem detail: `{"type", "title", "status", "detail"}`. */
export interface ProblemDetail {
  type: string
  title: string
  status: number
  detail?: string
  [key: string]: unknown
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

export interface LoginRequest {
  email: string
  password: string
  /** A TOTP code or a recovery code, when the account has MFA on (AUTH-2). */
  otp?: string
}

/** `GET /auth/mfa` (AUTH-2). */
export interface MfaStatus {
  enabled: boolean
  /** A seed from setup is waiting to be confirmed. */
  pending: boolean
  recovery_codes_left: number
  /** False for single sign-on and service accounts. */
  available: boolean
}

/** `POST /auth/mfa/setup`: what to add to the authenticator app. */
export interface MfaSetup {
  secret: string
  otpauth_uri: string
}

/** Recovery codes, shown once. */
export interface MfaRecoveryCodes {
  recovery_codes: string[]
}

export interface LoginResponse {
  access_token: string
  token_type: string
  expires_in: number
  /** AUTH-2 policy: the token only reaches the MFA routes until MFA is on. */
  mfa_setup_required: boolean
}

/** The configured single sign-on provider, as `/auth/providers` reports it (AUTH-1). */
export interface OidcProviderInfo {
  display_name: string
  /** Path under the API base the browser navigates to; accepts `?next=<path>`. */
  login_path: string
}

/** Which sign-in methods this installation offers. */
export interface AuthProviders {
  local: boolean
  oidc: OidcProviderInfo | null
}

// ---------------------------------------------------------------------------
// Dashboard (UX-5) — `GET /projects/{id}/stats`
// ---------------------------------------------------------------------------

export interface TaskTypeStats {
  open: number
  in_progress: number
  done: number
  cancelled: number
}

export interface ThroughputDay {
  /** ISO calendar date (UTC). */
  day: string
  submitted: number
  approved: number
  rejected: number
}

export interface ClassCount {
  label: string
  count: number
}

export interface AnnotatorStats {
  user_id: string
  display_name: string
  submitted: number
  approved: number
  rejected: number
}

export interface ProjectStats {
  items: { total: number; by_status: Record<ItemStatus, number> }
  tasks: { annotate: TaskTypeStats; review: TaskTypeStats }
  annotations: {
    versions: number
    by_source: Partial<Record<AnnotationSource, number>>
    latest_by_status: Record<AnnotationStatus, number>
  }
  review: { approved: number; rejected: number; rejection_rate: number }
  throughput: ThroughputDay[]
  classes: ClassCount[]
  annotators: AnnotatorStats[]
}

// ---------------------------------------------------------------------------
// API keys (AUTH-4)
// ---------------------------------------------------------------------------

export type ApiKeyScope = 'read' | 'write' | 'admin'

/** Key metadata; the bearer token itself is only returned at creation. */
export interface ApiKey {
  id: string
  organization_id: string
  user_id: string
  name: string
  scopes: ApiKeyScope[]
  expires_at: string | null
  last_used_at: string | null
  revoked_at: string | null
  created_by: string | null
  created_at: string
}

export interface ApiKeyCreate {
  name: string
  scopes: ApiKeyScope[]
  expires_at?: string | null
  /** Superuser only: mint the key for another user or a service account. */
  user_id?: string
}

/** `POST /service-accounts` (AUTH-4, superuser). */
/** Body of `POST /users/{id}/erase` (SEC-6). */
export interface EraseRequest {
  confirm_email: string
  redact_comments?: boolean
}

export interface ServiceAccountCreate {
  display_name: string
}

/** The freshly minted key, carrying `token` exactly once. */
export interface ApiKeyCreated extends ApiKey {
  token: string
}

// ---------------------------------------------------------------------------
// Webhooks (API-4)
// ---------------------------------------------------------------------------

export const WEBHOOK_EVENTS = [
  'annotation.submitted',
  'annotation.approved',
  'annotation.rejected',
  'item.approved',
  'snapshot.created',
  'job.succeeded',
  'job.failed',
  'retrain.requested',
  'webhook.test',
] as const
export type WebhookEvent = (typeof WEBHOOK_EVENTS)[number] | '*'

export interface Webhook {
  id: string
  organization_id: string
  project_id: string | null
  url: string
  description: string | null
  events: string[]
  is_active: boolean
  /** `json`: the signed event; `slack` / `teams`: a chat message (API-7). */
  format: WebhookFormat
  created_by_id: string | null
  last_delivery_at: string | null
  last_response_status: number | null
  created_at: string
  updated_at: string
}

/** Creation / rotation response: `secret` is shown exactly once. */
export interface WebhookWithSecret extends Webhook {
  secret: string | null
}

export type WebhookFormat = 'json' | 'slack' | 'teams'

export interface WebhookCreate {
  url: string
  events: string[]
  project_id?: string | null
  description?: string | null
  is_active?: boolean
  format?: WebhookFormat
}

export interface WebhookUpdate {
  url?: string
  events?: string[]
  description?: string | null
  is_active?: boolean
  format?: WebhookFormat
  rotate_secret?: boolean
}

/** `PATCH /auth/me`: the caller's own preferences (API-7). */
export interface UserPreferencesUpdate {
  email_notifications?: boolean
}

export type WebhookDeliveryStatus = 'pending' | 'succeeded' | 'failed'

export interface WebhookDelivery {
  id: string
  webhook_id: string
  event: string
  payload: Record<string, unknown>
  status: WebhookDeliveryStatus
  attempts: number
  next_attempt_at: string
  response_status: number | null
  error: string | null
  delivered_at: string | null
  created_at: string
}

export interface RetrainRequest {
  snapshot_id?: string
  model_id?: string
  note?: string
  /** A Databricks platform with `config.job_id`: start that job too (API-6). */
  ml_platform_id?: string
}

export interface RetrainResult {
  event: string
  deliveries: number
  ml_run: MlRun | null
}

// ---------------------------------------------------------------------------
// ML platforms — MLflow, Databricks, Azure ML (API-6)
// ---------------------------------------------------------------------------

export type MlPlatformKind = 'mlflow' | 'databricks' | 'azureml'

export type MlIdentity = 'none' | 'bearer' | 'basic' | 'service_principal' | 'managed_identity'

/** `config`: `client_id` / `tenant_id` for identities, `job_id` for a Databricks retrain job. */
export interface MlPlatformConfig {
  /** MLflow only: where people open the UI when it differs from `tracking_uri`. */
  ui_url?: string
  client_id?: string
  tenant_id?: string
  job_id?: number
}

/** `GET /ml-platforms`. Never carries `secret_ref`, only `has_secret`. */
export interface MlPlatform {
  id: string
  organization_id: string
  name: string
  kind: MlPlatformKind
  tracking_uri: string
  identity_type: MlIdentity
  has_secret: boolean
  config: MlPlatformConfig
  created_at: string
  updated_at: string
}

export interface MlPlatformCreate {
  name: string
  kind: MlPlatformKind
  tracking_uri: string
  identity_type: MlIdentity
  secret_ref?: string | null
  config?: MlPlatformConfig
}

export interface MlPlatformCheck {
  ok: boolean
  messages: string[]
  info: { tracking_uri?: string; experiments?: string[] } | null
}

/** `POST /projects/{id}/snapshots/{sid}/mlflow`. */
export interface SnapshotPublishRequest {
  ml_platform_id: string
  experiment?: string
}

export interface SnapshotPublishResult {
  ml_platform_id: string
  experiment_id: string
  experiment_name: string
  run_id: string
  run_url: string | null
  /** False when the snapshot already had a run in that experiment. */
  created: boolean
}

/** `POST /models/{id}/versions/import`: `run_id`, or `registered_model` + `model_version`. */
export interface ModelVersionImport {
  ml_platform_id: string
  run_id?: string
  registered_model?: string
  model_version?: string
  version?: number
  class_mapping?: Record<string, string | null>
}

export interface MlRun {
  ml_platform_id: string
  run_id: string
  run_url: string | null
}

// ---------------------------------------------------------------------------
// Licence (LIC-1, LIC-24, LIC-26)
// ---------------------------------------------------------------------------

export type LicenseStatus = 'community' | 'valid' | 'expired' | 'invalid'

/** A Business feature id (LIC-32); what a key's `features` unlocks. */
export type BusinessFeature =
  | 'sso'
  | 'scim'
  | 'path_permissions'
  | 'audit_history'
  | 'seat_report'
  | 'sharepoint'
  | 'ml_platforms'
  | 'quality'

/** `GET /license`. Never carries a key. */
export interface LicenseInfo {
  status: LicenseStatus
  /** The edition: `community` without a key in force, else `team`, `business`, `enterprise` or `trial`. */
  tier: string
  licensee: string | null
  seats: number | null
  expires_at: string | null
  /** The Business features the licence in force unlocks (LIC-33). */
  features: BusinessFeature[]
  /** Every Business feature, so locked ones can be shown. */
  business_features: BusinessFeature[]
  /** Community with more users active than its limit: only the owner signs in (LIC-36). */
  owner_only: boolean
  /** The stored key is a trial key, current or expired (LIC-34). */
  trial_used: boolean
  license_id: string | null
  /** Where the licence in force came from. */
  source: 'env' | 'admin' | 'refresh' | 'trial' | null
  /** Distinct human users active in the last 30 days. */
  active_users: number
  /** Seats plus overage; `null` when the build enforces no limit. */
  seat_limit: number | null
  /** Last day of the 30-day grace after expiry (LIC-5); `null` without a key. */
  grace_ends_at: string | null
  /** Annotation and new tasks are refused until a renewed key is installed. */
  restricted: boolean
  /** Hosts the key is bound to (LIC-29); empty when unbound or without a key. */
  hosts: string[]
  /** This install is reached on a host outside `hosts`: the expired path. */
  host_mismatch: boolean
  /** The licence was revoked (LIC-8): its last valid day. */
  revoked_at: string | null
}

/** One calendar month of `GET /license/seat-report`, clipped to the range (LIC-30). */
export interface SeatReportPeriod {
  start: string
  end: string
  /** Distinct users active at any moment of the period. */
  active_users: number
  /** The most users active at one moment; `peak_at` is `null` when zero. */
  peak_active_users: number
  peak_at: string | null
  /** Peak over the licensed seats; `null` without a key. */
  overage: number | null
}

/** `GET /license/seat-report`: active users per month for offline true-up. Not signed. */
export interface SeatReport {
  generated_at: string
  install_id: string | null
  license_id: string | null
  licensee: string | null
  tier: string
  seats: number | null
  seat_limit: number | null
  start: string
  end: string
  peak_active_users: number
  peak_overage: number | null
  periods: SeatReportPeriod[]
}

/** One sign of seat sharing (LIC-31); a notice, never a block. */
export interface UsageNotice {
  kind: 'parallel_sign_ins' | 'parallel_tasks' | 'superhuman_pace' | 'service_account_annotating'
  user_id: string
  email: string
  display_name: string
  detail: string
  count: number
}

/** `GET /license/usage-notices`. */
export interface UsageNotices {
  window_days: number
  notices: UsageNotice[]
}

/** `GET` / `POST /license/refresh` (LIC-27). */
export interface LicenseRefreshStatus {
  /** `APP_LICENSE_REFRESH_ENABLED`. */
  enabled: boolean
  /** `APP_LICENSE_SERVER_URL` is set; without it nothing is sent. */
  server_configured: boolean
  /** What a refresh would send now; `null` without a verified key. */
  payload: Record<string, unknown> | null
  attempted_at: string | null
  succeeded_at: string | null
  error: string | null
  /** The body of the last refresh, as sent. */
  last_payload: Record<string, unknown> | null
}

/** `GET /licensing/telemetry/preview`: the heartbeat, byte for byte (LIC-6, LIC-21). */
export interface TelemetryPreview {
  enabled: boolean
  install_id: string | null
  version: string
  licence_type: string
  active_users: number
  fingerprint: Record<string, unknown>
  /** What the fingerprint left out and why; shown here, never sent. */
  withheld: string[]
  notice: string | null
  server_configured: boolean
  attempted_at: string | null
  sent_at: string | null
  error: string | null
  last_payload: Record<string, unknown> | null
}

export interface LicenseKeyUpdate {
  key: string
}

// ---------------------------------------------------------------------------
// Quality control (QA-1 … QA-4, IMG-6) — CONTRACTS.md "Quality control"
// ---------------------------------------------------------------------------

export interface AgreementAnnotator {
  user_id: string
  email: string | null
  display_name: string | null
  items: number
}

export interface ClassificationAgreement {
  field: string
  items: number
  fleiss_kappa: number | null
  krippendorff_alpha: number | null
}

export interface AgreementPair {
  a: string
  b: string
  items?: number
  cohen_kappa: number | null
  mean_iou: number | null
  shape_f1: number | null
  span_f1_exact: number | null
  span_f1_overlap: number | null
}

export interface ItemAgreement {
  annotators: AgreementAnnotator[]
  classification: ClassificationAgreement[]
  shapes: {
    mean_iou: number | null
    f1: number | null
    iou_threshold: number
    envelope_iou: boolean
  }
  spans: { f1_exact: number | null; f1_overlap: number | null }
  pairs: AgreementPair[]
}

export interface ProjectAgreement extends ItemAgreement {
  items: number
}

export interface ConsensusAnnotator {
  user_id: string
  email: string | null
  display_name: string | null
  annotation_id: string
  version: number
  status: AnnotationStatus
  created_at: string
}

export interface ConsensusView {
  expected: number
  annotators: ConsensusAnnotator[]
  agreement: ItemAgreement
  preview: AnnotationResult
  conflicts: string[]
}

export type ConsensusResolve =
  | { method: 'pick'; annotation_id: string; comment?: string }
  | { method: 'fuse'; iou_threshold?: number; min_votes?: number; comment?: string }

export interface GoldTasksRequest {
  user_ids?: string[]
  item_ids?: string[]
  priority?: number
}

export interface GoldTasksResult {
  opened: number
  skipped: number
}

export interface AnnotatorAccuracy {
  user_id: string
  email: string | null
  display_name: string | null
  gold_items: number
  classification_accuracy: number | null
  shape_f1: number | null
  mean_iou: number | null
  span_f1: number | null
  score: number | null
}

export type SplitRequest =
  { grid: { rows: number; cols: number; overlap_px?: number } } | { regions: BBox[] }

export interface SplitResult {
  tasks: Task[]
}

// ---------------------------------------------------------------------------
// Deep Zoom tile pyramid (IMG-1)
// ---------------------------------------------------------------------------

/**
 * `item.meta.tiles`: present once the `tile_image` job has tiled a large
 * image into a Deep Zoom pyramid. `path` is the connector prefix the tile
 * files live under (`{path}image_files/{level}/{col}_{row}.{suffix}`).
 */
export interface ItemTiles {
  format: 'dzi'
  path: string
  tile_size: number
  overlap: number
  suffix: string
  max_level: number
  width: number
  height: number
}

/** One tile address: `[level, col, row]`. */
export type TileKey = [number, number, number]

/** Body of `POST /items/{id}/tiles/sign` (IMG-1); 1-512 tiles per call. */
export interface SignTilesRequest {
  tiles: TileKey[]
}

/** Signed read URLs, same order as the request. */
export interface SignTilesResponse {
  urls: string[]
  expires_in: number
}
