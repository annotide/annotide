/**
 * Typed fetch wrapper for the backend REST API (docs/CONTRACTS.md, "REST API").
 *
 * - Base URL: `import.meta.env.VITE_API_BASE_URL ?? '/api/v1'`.
 * - Attaches `Authorization: Bearer <token>` from the auth store when present.
 * - Parses RFC 9457 problem details into a thrown `ApiError`.
 * - Supports cursor pagination and an optional `Idempotency-Key` header on creates.
 */
import { claimSilentAttempt, currentPath, isSsoToken } from '@/lib/sso'
import { useAuthStore } from '@/lib/store'
import type {
  MlPlatform,
  MlPlatformCheck,
  MlPlatformCreate,
  ModelVersionImport,
  SnapshotPublishRequest,
  SnapshotPublishResult,
  AnnotatorAccuracy,
  ConsensusResolve,
  ConsensusView,
  GoldTasksRequest,
  GoldTasksResult,
  ProjectAgreement,
  SplitRequest,
  SplitResult,
  Annotation,
  BulkRequest,
  BulkResult,
  AnnotationResult,
  Comment,
  CorrectionMetrics,
  CommentCreate,
  Connector,
  ConnectorCheck,
  ConnectorCreate,
  ConnectorEventToken,
  ConnectorUpdate,
  ExportDownload,
  ApiKey,
  ApiKeyCreate,
  ApiKeyCreated,
  ProjectStats,
  ImportRequest,
  ImportUploadOptions,
  Item,
  ItemView,
  ItemStatus,
  Job,
  LabelSchemaDefinition,
  LabelSchemaVersion,
  LoginRequest,
  MfaRecoveryCodes,
  MfaSetup,
  MfaStatus,
  LoginResponse,
  Member,
  MemberCreate,
  Model,
  InteractiveRequest,
  InteractiveResult,
  OcrRequest,
  OcrResult,
  ModelCheck,
  ModelCreate,
  ModelFamily,
  ModelUpdate,
  ModelVersion,
  ModelVersionCreate,
  Notification,
  Page,
  PrelabelRequest,
  ProblemDetail,
  Project,
  ProjectCreate,
  ProjectRole,
  ProjectUpdate,
  RetrainRequest,
  RetrainResult,
  Snapshot,
  EraseRequest,
  ServiceAccountCreate,
  SignTilesResponse,
  SnapshotCreate,
  SnapshotDiff,
  SnapshotLineage,
  Task,
  TaskStatus,
  TaskType,
  TaskUpdate,
  TileKey,
  UploadTarget,
  UploadUrlsRequest,
  UploadUrlsResponse,
  User,
  UserPreferencesUpdate,
  LicenseInfo,
  LicenseKeyUpdate,
  SeatReport,
  LicenseRefreshStatus,
  TelemetryPreview,
  UsageNotices,
  Webhook,
  WebhookCreate,
  WebhookDelivery,
  WebhookUpdate,
  WebhookWithSecret,
  AuthProviders,
  OidcProviderInfo,
  ScimToken,
  ScimTokenState,
} from './types'

const DEFAULT_BASE_URL = '/api/v1'

function getBaseUrl(): string {
  const configured = import.meta.env.VITE_API_BASE_URL
  return configured && configured.length > 0 ? configured : DEFAULT_BASE_URL
}

/** Thrown for any non-2xx response. Carries the parsed RFC 9457 problem detail. */
export class ApiError extends Error {
  readonly status: number
  readonly title: string
  readonly detail?: string
  readonly type: string
  readonly problem: ProblemDetail

  constructor(problem: ProblemDetail) {
    super(problem.detail ?? problem.title ?? `Request failed with status ${problem.status}`)
    this.name = 'ApiError'
    this.status = problem.status
    this.title = problem.title
    this.detail = problem.detail
    this.type = problem.type
    this.problem = problem
  }
}

async function parseProblemDetail(response: Response): Promise<ProblemDetail> {
  try {
    const body = (await response.json()) as Partial<ProblemDetail>
    return {
      type: body.type ?? 'about:blank',
      title: body.title ?? response.statusText,
      status: body.status ?? response.status,
      detail: body.detail,
      ...body,
    }
  } catch {
    return {
      type: 'about:blank',
      title: response.statusText || 'Request failed',
      status: response.status,
    }
  }
}

export interface RequestOptions {
  /** Query parameters appended to the URL. Undefined values are omitted. */
  query?: Record<string, string | number | boolean | undefined>
  /** Attaches an `Idempotency-Key` header, for creation endpoints. */
  idempotencyKey?: string
  /** Aborts the request if the signal fires. */
  signal?: AbortSignal
  /**
   * Use this bearer token instead of the one in the auth store.
   *
   * Needed exactly once, at sign-in: the token has just been issued and the
   * store is still empty, so the profile fetch has nothing to read yet.
   */
  token?: string
}

/**
 * A 401 on an authenticated call: the token expired or was revoked. An SSO
 * session tries one silent re-authentication at the provider (`prompt=none`)
 * and comes back to the same page; anything else is signed out so the route
 * guard shows the login form.
 */
function handleUnauthorized(): void {
  const { token, logout } = useAuthStore.getState()
  const silent = isSsoToken(token) && claimSilentAttempt()
  logout()
  if (silent) {
    window.location.assign(
      `${getBaseUrl()}/auth/oidc/login?prompt=none&next=${encodeURIComponent(currentPath())}`,
    )
  }
}

function buildUrl(path: string, query?: RequestOptions['query']): string {
  const url = new URL(getBaseUrl() + path, window.location.origin)
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined) url.searchParams.set(key, String(value))
    }
  }
  // Return an origin-relative URL when the base is relative, so requests
  // still go through the Vite dev proxy in development.
  return url.pathname + url.search
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  options?: RequestOptions,
): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  const isForm = body instanceof FormData
  if (body !== undefined && !isForm) headers['Content-Type'] = 'application/json'

  const token = options?.token ?? useAuthStore.getState().token
  if (token) headers.Authorization = `Bearer ${token}`

  if (options?.idempotencyKey) headers['Idempotency-Key'] = options.idempotencyKey

  const response = await fetch(buildUrl(path, options?.query), {
    method,
    headers,
    body: isForm ? body : body !== undefined ? JSON.stringify(body) : undefined,
    signal: options?.signal,
  })

  if (!response.ok) {
    const problem = await parseProblemDetail(response)

    // An expired or revoked token must not leave the app wedged on a screen
    // full of failed queries. Drop it so the route guard sends the person to
    // sign in. Skipped when an explicit token was supplied: that is the
    // sign-in attempt itself, and clearing the store there would be pointless.
    if (response.status === 401 && !options?.token) {
      handleUnauthorized()
    }

    throw new ApiError(problem)
  }

  if (response.status === 204) return undefined as T
  const text = await response.text()
  if (!text) return undefined as T
  return JSON.parse(text) as T
}

/**
 * Multipart upload via `XMLHttpRequest` instead of `fetch` — the only way to
 * observe upload progress in the browser. Mirrors `request()`'s base URL,
 * bearer auth header and RFC 9457 error handling (including the 401 logout).
 */
function uploadWithProgress<T>(
  path: string,
  form: FormData,
  onProgress?: (fraction: number) => void,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', buildUrl(path))
    xhr.setRequestHeader('Accept', 'application/json')

    const token = useAuthStore.getState().token
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)

    if (onProgress) {
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable) onProgress(event.loaded / event.total)
      }
    }

    xhr.onload = () => {
      try {
        const status = xhr.status
        if (status >= 200 && status < 300) {
          if (status === 204 || !xhr.responseText) {
            resolve(undefined as T)
          } else {
            resolve(JSON.parse(xhr.responseText) as T)
          }
          return
        }

        let problem: ProblemDetail
        try {
          const parsed = xhr.responseText
            ? (JSON.parse(xhr.responseText) as Partial<ProblemDetail>)
            : {}
          problem = {
            type: parsed.type ?? 'about:blank',
            title: parsed.title ?? (xhr.statusText || 'Request failed'),
            status: parsed.status ?? status,
            detail: parsed.detail,
            ...parsed,
          }
        } catch {
          problem = {
            type: 'about:blank',
            title: xhr.statusText || 'Request failed',
            status,
          }
        }

        if (status === 401) handleUnauthorized()
        reject(new ApiError(problem))
      } catch (err) {
        reject(err instanceof Error ? err : new Error(String(err)))
      }
    }

    xhr.onerror = () => {
      reject(new ApiError({ type: 'about:blank', title: 'Network error', status: 0 }))
    }

    xhr.send(form)
  })
}

export interface CursorPage {
  limit?: number
  cursor?: string
  [key: string]: string | number | boolean | undefined
}

export interface ListItemsFilters extends CursorPage {
  status?: ItemStatus
  /** Exact `meta.tags` entry (set by bulk `tag`). */
  tag?: string
}

export interface ListTasksFilters extends CursorPage {
  status?: TaskStatus
  type?: TaskType
  assignee_id?: string
}

// ---------------------------------------------------------------------------
// Endpoint methods
// ---------------------------------------------------------------------------

export const api = {
  // Auth
  login(payload: LoginRequest): Promise<LoginResponse> {
    return request<LoginResponse>('POST', '/auth/login', payload)
  },
  getMfa(): Promise<MfaStatus> {
    return request<MfaStatus>('GET', '/auth/mfa')
  },
  setupMfa(): Promise<MfaSetup> {
    return request<MfaSetup>('POST', '/auth/mfa/setup')
  },
  enableMfa(code: string): Promise<MfaRecoveryCodes> {
    return request<MfaRecoveryCodes>('POST', '/auth/mfa/enable', { code })
  },
  replaceRecoveryCodes(code: string): Promise<MfaRecoveryCodes> {
    return request<MfaRecoveryCodes>('POST', '/auth/mfa/recovery-codes', { code })
  },
  disableMfa(code: string): Promise<void> {
    return request<void>('DELETE', '/auth/mfa', { code })
  },
  /** Everything held about one person (SEC-6); the JSON is saved as-is. */
  exportPersonalData(userId: string): Promise<Record<string, unknown>> {
    return request<Record<string, unknown>>('GET', `/users/${userId}/personal-data`)
  },
  listUsers(q?: string): Promise<User[]> {
    return request<User[]>('GET', '/users', undefined, { query: { q: q || undefined } })
  },
  eraseUser(userId: string, payload: EraseRequest): Promise<User> {
    return request<User>('POST', `/users/${userId}/erase`, payload)
  },
  me(token?: string): Promise<User> {
    return request<User>('GET', '/auth/me', undefined, token ? { token } : undefined)
  },
  updateMe(body: UserPreferencesUpdate): Promise<User> {
    return request<User>('PATCH', '/auth/me', body)
  },
  authProviders(): Promise<AuthProviders> {
    return request<AuthProviders>('GET', '/auth/providers')
  },
  /**
   * Absolute URL that starts single sign-on (AUTH-1). A full-page navigation,
   * not a request: the backend sets a flow cookie and redirects to the IdP.
   */
  /** Full-page navigation that ends the identity provider's session too. */
  oidcLogoutUrl(): string {
    return `${getBaseUrl()}/auth/oidc/logout`
  },
  oidcLoginUrl(provider: OidcProviderInfo, next?: string): string {
    const query = next && next !== '/' ? `?next=${encodeURIComponent(next)}` : ''
    return `${getBaseUrl()}${provider.login_path}${query}`
  },

  // Projects
  listProjects(paging?: CursorPage): Promise<Page<Project>> {
    return request<Page<Project>>('GET', '/projects', undefined, {
      query: paging,
    })
  },
  getProject(id: string): Promise<Project> {
    return request<Project>('GET', `/projects/${id}`)
  },
  createProject(payload: ProjectCreate, idempotencyKey?: string): Promise<Project> {
    return request<Project>('POST', '/projects', payload, { idempotencyKey })
  },
  updateProject(id: string, payload: ProjectUpdate): Promise<Project> {
    return request<Project>('PATCH', `/projects/${id}`, payload)
  },
  deleteProject(id: string): Promise<void> {
    return request<void>('DELETE', `/projects/${id}`)
  },

  // Project members (owner only for writes)
  listMembers(projectId: string): Promise<Member[]> {
    return request<Member[]>('GET', `/projects/${projectId}/members`)
  },
  addMember(projectId: string, payload: MemberCreate): Promise<Member> {
    return request<Member>('POST', `/projects/${projectId}/members`, payload)
  },
  updateMember(projectId: string, userId: string, role: ProjectRole): Promise<Member> {
    return request<Member>('PATCH', `/projects/${projectId}/members/${userId}`, { role })
  },
  /** Limit a member to folders (§4); `null` gives the whole project back. */
  updateMemberFolders(
    projectId: string,
    userId: string,
    pathPrefixes: string[] | null,
  ): Promise<Member> {
    return request<Member>('PATCH', `/projects/${projectId}/members/${userId}`, {
      path_prefixes: pathPrefixes,
    })
  },
  removeMember(projectId: string, userId: string): Promise<void> {
    return request<void>('DELETE', `/projects/${projectId}/members/${userId}`)
  },

  // Items
  listItems(projectId: string, filters?: ListItemsFilters): Promise<Page<Item>> {
    return request<Page<Item>>('GET', `/projects/${projectId}/items`, undefined, {
      query: filters,
    })
  },
  getItem(id: string): Promise<Item> {
    return request<Item>('GET', `/items/${id}`)
  },
  /**
   * Read a text item's content through its signed media URL (TOOL). The API
   * never proxies media, so this goes to the storage URL directly, without
   * the bearer token.
   */
  async fetchMediaText(url: string, signal?: AbortSignal): Promise<string> {
    const response = await fetch(url, { signal })
    if (!response.ok) {
      throw new ApiError({
        type: 'about:blank',
        title: 'Could not load text',
        status: response.status,
        detail: `The storage answered ${response.status}.`,
      })
    }
    return response.text()
  },
  /** The item's companion views with signed URLs (§5 multimodal). */
  getItemViews(itemId: string): Promise<ItemView[]> {
    return request<ItemView[]>('GET', `/items/${itemId}/views`)
  },
  /** An item's media as bytes, from its signed URL (audio waveforms, §5). */
  async fetchMediaBytes(url: string, signal?: AbortSignal): Promise<ArrayBuffer> {
    const response = await fetch(url, { signal })
    if (!response.ok) {
      throw new ApiError({
        type: 'about:blank',
        title: 'Could not load media',
        status: response.status,
        detail: `The storage answered ${response.status}.`,
      })
    }
    return response.arrayBuffer()
  },
  /** Split an image item into region tasks (IMG-6). Owner or reviewer. */
  splitItem(itemId: string, body: SplitRequest): Promise<SplitResult> {
    return request<SplitResult>('POST', `/items/${itemId}/split`, body)
  },
  /**
   * Signed read URLs for tiles of an item's DZI pyramid (IMG-1), 1-512 per
   * call. 409 when the item has no `meta.tiles`; 422 for an out-of-range
   * `[level, col, row]`.
   */
  signTiles(itemId: string, tiles: TileKey[]): Promise<SignTilesResponse> {
    return request<SignTilesResponse>('POST', `/items/${itemId}/tiles/sign`, { tiles })
  },
  /** Consensus versions, agreement and fuse preview for one item (QA-1…QA-3). */
  getConsensus(itemId: string): Promise<ConsensusView> {
    return request<ConsensusView>('GET', `/items/${itemId}/consensus`)
  },
  resolveConsensus(itemId: string, body: ConsensusResolve): Promise<Annotation> {
    return request<Annotation>('POST', `/items/${itemId}/consensus/resolve`, body)
  },
  /** Mark an approved primary version as the item's gold reference (QA-4). */
  setGold(itemId: string, annotationId: string): Promise<Item> {
    return request<Item>('PUT', `/items/${itemId}/gold`, { annotation_id: annotationId })
  },
  clearGold(itemId: string): Promise<Item> {
    return request<Item>('DELETE', `/items/${itemId}/gold`)
  },
  openGoldTasks(projectId: string, body: GoldTasksRequest = {}): Promise<GoldTasksResult> {
    return request<GoldTasksResult>('POST', `/projects/${projectId}/gold/tasks`, body)
  },
  getAgreement(projectId: string): Promise<ProjectAgreement> {
    return request<ProjectAgreement>('GET', `/projects/${projectId}/agreement`)
  },
  getAnnotatorQuality(projectId: string): Promise<{ annotators: AnnotatorAccuracy[] }> {
    return request<{ annotators: AnnotatorAccuracy[] }>(
      'GET',
      `/projects/${projectId}/quality/annotators`,
    )
  },
  /** Scan with the project's own connector, prefix and glob; the route requires a body. */
  scanProject(projectId: string): Promise<Job> {
    return request<Job>('POST', `/projects/${projectId}/scan`, {})
  },
  /** Forget and regenerate thumbnails and tiles (SRC-6); `purge` deletes the old blobs first. */
  rebuildCache(projectId: string, purge: boolean): Promise<Job> {
    return request<Job>('POST', `/projects/${projectId}/cache/rebuild`, { purge })
  },
  /**
   * Retry the project's PDF texts that are pending or failed (PDF text mode);
   * `force` also re-extracts ready ones nobody has annotated.
   */
  extractPdfText(projectId: string, force: boolean): Promise<Job> {
    return request<Job>('POST', `/projects/${projectId}/extract-text`, { force })
  },
  /** Mint write-scoped signed URLs on the project's source connector (§12 upload). */
  createUploadUrls(projectId: string, payload: UploadUrlsRequest): Promise<UploadUrlsResponse> {
    return request<UploadUrlsResponse>('POST', `/projects/${projectId}/uploads`, payload)
  },
  /**
   * PUT one file's bytes to a signed upload URL. Deliberately outside `request`:
   * the target is the customer's store (or the local proxy), not the API, so it
   * gets no bearer token, no `Accept: application/json` and no problem-detail
   * parsing — a non-2xx is surfaced with the store's status text.
   */
  async putToSignedUrl(target: UploadTarget, file: Blob): Promise<void> {
    const response = await fetch(target.url, {
      method: target.method,
      headers: target.headers,
      body: file,
    })
    if (!response.ok) {
      throw new Error(`Upload of ${target.path} failed: ${response.status} ${response.statusText}`)
    }
  },
  /** Queue thumbnail generation for the project's images (IMG-8). */
  createThumbnails(projectId: string, payload?: { force?: boolean }): Promise<Job> {
    return request<Job>('POST', `/projects/${projectId}/thumbnails`, payload ?? {})
  },

  // Tasks
  listTasks(projectId: string, filters?: ListTasksFilters): Promise<Page<Task>> {
    return request<Page<Task>>('GET', `/projects/${projectId}/tasks`, undefined, {
      query: filters,
    })
  },
  /** One action on many items (WF-8). Owner or reviewer. */
  bulkItems(projectId: string, body: BulkRequest): Promise<BulkResult> {
    return request<BulkResult>('POST', `/projects/${projectId}/items/bulk`, body)
  },
  /** Re-prioritise / re-schedule / re-assign a live task (WF-6). Owner or reviewer. */
  updateTask(id: string, patch: TaskUpdate): Promise<Task> {
    return request<Task>('PATCH', `/tasks/${id}`, patch)
  },
  /**
   * Claim the next open task and take its lock (WF-2, WF-3). Resolves to
   * `null` when the queue is empty (the API answers 204).
   */
  async nextTask(payload: { project_id: string; type?: Task['type'] }): Promise<Task | null> {
    // An empty queue is a 204, which `request` parses as `undefined`.
    const task = await request<Task | undefined>('POST', '/tasks/next', undefined, {
      query: { project_id: payload.project_id, type: payload.type },
    })
    return task ?? null
  },
  releaseTask(id: string): Promise<Task> {
    return request<Task>('POST', `/tasks/${id}/release`)
  },
  extendTask(id: string): Promise<Task> {
    return request<Task>('POST', `/tasks/${id}/extend`)
  },

  // Annotations
  /** Version history, newest first. A plain array, not a page (see CONTRACTS.md). */
  listAnnotations(itemId: string): Promise<Annotation[]> {
    return request<Annotation[]>('GET', `/items/${itemId}/annotations`)
  },
  createAnnotation(
    itemId: string,
    payload: {
      result: AnnotationResult
      label_schema_version_id: string
      task_id?: string
      duration_ms?: number
      /** Submit straight away instead of saving a draft (runs QA-6 validation). */
      submit?: boolean
    },
    idempotencyKey?: string,
  ): Promise<Annotation> {
    return request<Annotation>('POST', `/items/${itemId}/annotations`, payload, {
      idempotencyKey,
    })
  },
  submitAnnotation(id: string): Promise<Annotation> {
    return request<Annotation>('POST', `/annotations/${id}/submit`)
  },
  reviewAnnotation(
    id: string,
    payload: {
      approve: boolean
      comment?: string
      corrected_result?: AnnotationResult
    },
  ): Promise<Annotation> {
    return request<Annotation>('POST', `/annotations/${id}/review`, payload)
  },

  // Schemas
  /** Newest version first. The API returns a bare list, not a page. */
  listSchemas(projectId: string): Promise<LabelSchemaVersion[]> {
    return request<LabelSchemaVersion[]>('GET', `/projects/${projectId}/schemas`)
  },
  /** Versions are immutable (TOOL-4): this always creates the next one. Owner only. */
  createSchemaVersion(
    projectId: string,
    definition: LabelSchemaDefinition,
  ): Promise<LabelSchemaVersion> {
    return request<LabelSchemaVersion>('POST', `/projects/${projectId}/schemas`, definition)
  },

  // Exports / jobs
  createExport(
    projectId: string,
    payload?: Record<string, unknown>,
    idempotencyKey?: string,
  ): Promise<Job> {
    return request<Job>('POST', `/projects/${projectId}/exports`, payload, {
      idempotencyKey,
    })
  },
  createImport(projectId: string, payload: ImportRequest): Promise<Job> {
    return request<Job>('POST', `/projects/${projectId}/imports`, payload)
  },
  /**
   * Multipart upload of an annotation file or archive, queued as an import
   * job (EXP-6). Goes through `XMLHttpRequest`, not `request()`, so callers
   * can observe upload progress via `onProgress` (a 0-1 fraction).
   */
  uploadImport(
    projectId: string,
    file: File,
    options: ImportUploadOptions,
    onProgress?: (fraction: number) => void,
  ): Promise<Job> {
    const form = new FormData()
    form.append('file', file)
    form.append('format', options.format)
    if (options.status) form.append('status', options.status)
    if (options.dry_run !== undefined) form.append('dry_run', String(options.dry_run))
    if (options.class_mapping) form.append('class_mapping', JSON.stringify(options.class_mapping))
    if (options.attribute_mapping && Object.keys(options.attribute_mapping).length > 0) {
      form.append('attribute_mapping', JSON.stringify(options.attribute_mapping))
    }
    return uploadWithProgress<Job>(`/projects/${projectId}/imports/upload`, form, onProgress)
  },
  getJob(id: string): Promise<Job> {
    return request<Job>('GET', `/jobs/${id}`)
  },
  /** Re-queue a failed or cancelled job with the same payload (ARC-4); 409 otherwise. */
  retryJob(id: string): Promise<Job> {
    return request<Job>('POST', `/jobs/${id}/retry`)
  },
  listJobs(
    projectId: string,
    filters?: CursorPage & { status?: Job['status']; type?: Job['type'] },
  ): Promise<Page<Job>> {
    return request<Page<Job>>('GET', `/projects/${projectId}/jobs`, undefined, {
      query: filters,
    })
  },
  // Snapshots (EXP-1, EXP-3, EXP-4)
  listSnapshots(projectId: string, paging?: CursorPage): Promise<Page<Snapshot>> {
    return request<Page<Snapshot>>('GET', `/projects/${projectId}/snapshots`, undefined, {
      query: paging,
    })
  },
  /** Queue a snapshot job; the job's `result.snapshot_id` names the frozen set. */
  createSnapshot(projectId: string, body: SnapshotCreate): Promise<Job> {
    return request<Job>('POST', `/projects/${projectId}/snapshots`, body)
  },
  diffSnapshots(projectId: string, baseId: string, targetId: string): Promise<SnapshotDiff> {
    return request<SnapshotDiff>(
      'GET',
      `/projects/${projectId}/snapshots/${baseId}/diff/${targetId}`,
    )
  },
  snapshotLineage(projectId: string, snapshotId: string): Promise<SnapshotLineage> {
    return request<SnapshotLineage>('GET', `/projects/${projectId}/snapshots/${snapshotId}/lineage`)
  },
  // Licence (LIC-1, LIC-26); superuser only
  getLicense(): Promise<LicenseInfo> {
    return request<LicenseInfo>('GET', '/license')
  },
  installLicense(body: LicenseKeyUpdate): Promise<LicenseInfo> {
    return request<LicenseInfo>('PUT', '/license', body)
  },
  removeLicense(): Promise<LicenseInfo> {
    return request<LicenseInfo>('DELETE', '/license')
  },
  getLicenseRefresh(): Promise<LicenseRefreshStatus> {
    return request<LicenseRefreshStatus>('GET', '/license/refresh')
  },
  refreshLicenseNow(): Promise<LicenseRefreshStatus> {
    return request<LicenseRefreshStatus>('POST', '/license/refresh')
  },
  startTrial(): Promise<LicenseInfo> {
    return request<LicenseInfo>('POST', '/license/trial')
  },
  getUsageNotices(): Promise<UsageNotices> {
    return request<UsageNotices>('GET', '/license/usage-notices')
  },
  getTelemetryPreview(): Promise<TelemetryPreview> {
    return request<TelemetryPreview>('GET', '/licensing/telemetry/preview')
  },
  getSeatReport(range: { start?: string; end?: string }): Promise<SeatReport> {
    return request<SeatReport>('GET', '/license/seat-report', undefined, { query: range })
  },

  // Webhooks (API-4) and retraining (ML-9)
  listWebhooks(projectId?: string, paging?: CursorPage): Promise<Page<Webhook>> {
    return request<Page<Webhook>>('GET', '/webhooks', undefined, {
      query: { ...paging, project_id: projectId },
    })
  },
  createWebhook(body: WebhookCreate): Promise<WebhookWithSecret> {
    return request<WebhookWithSecret>('POST', '/webhooks', body)
  },
  updateWebhook(id: string, body: WebhookUpdate): Promise<WebhookWithSecret> {
    return request<WebhookWithSecret>('PATCH', `/webhooks/${id}`, body)
  },
  deleteWebhook(id: string): Promise<void> {
    return request<void>('DELETE', `/webhooks/${id}`)
  },
  testWebhook(id: string): Promise<WebhookDelivery> {
    return request<WebhookDelivery>('POST', `/webhooks/${id}/test`)
  },
  listWebhookDeliveries(id: string, paging?: CursorPage): Promise<Page<WebhookDelivery>> {
    return request<Page<WebhookDelivery>>('GET', `/webhooks/${id}/deliveries`, undefined, {
      query: paging,
    })
  },
  requestRetrain(projectId: string, body: RetrainRequest): Promise<RetrainResult> {
    return request<RetrainResult>('POST', `/projects/${projectId}/retrain`, body)
  },
  /** Signed URL for a succeeded export's archive (EXP-5). */
  downloadExport(jobId: string): Promise<ExportDownload> {
    return request<ExportDownload>('GET', `/jobs/${jobId}/download`)
  },

  // Dashboard (UX-5)
  getProjectStats(projectId: string, options?: { days?: number }): Promise<ProjectStats> {
    return request<ProjectStats>('GET', `/projects/${projectId}/stats`, undefined, {
      query: options,
    })
  },

  // Connectors
  listConnectors(): Promise<Page<Connector>> {
    return request<Page<Connector>>('GET', '/connectors')
  },
  createConnector(payload: ConnectorCreate, idempotencyKey?: string): Promise<Connector> {
    return request<Connector>('POST', '/connectors', payload, {
      idempotencyKey,
    })
  },
  updateConnector(id: string, payload: ConnectorUpdate): Promise<Connector> {
    return request<Connector>('PATCH', `/connectors/${id}`, payload)
  },
  deleteConnector(id: string): Promise<void> {
    return request<void>('DELETE', `/connectors/${id}`)
  },
  checkConnector(id: string): Promise<ConnectorCheck> {
    return request<ConnectorCheck>('POST', `/connectors/${id}/check`)
  },
  /** Turn storage events on or replace the token; the old one stops working (SRC-3). */
  mintConnectorEventToken(id: string): Promise<ConnectorEventToken> {
    return request<ConnectorEventToken>('POST', `/connectors/${id}/events/token`)
  },
  revokeConnectorEventToken(id: string): Promise<void> {
    return request<void>('DELETE', `/connectors/${id}/events/token`)
  },

  // SCIM provisioning (AUTH-3)
  getScimToken(): Promise<ScimTokenState> {
    return request<ScimTokenState>('GET', '/scim/token')
  },
  /** Turn SCIM on or replace the token; the old one stops working. */
  mintScimToken(): Promise<ScimToken> {
    return request<ScimToken>('POST', '/scim/token')
  },
  revokeScimToken(): Promise<void> {
    return request<void>('DELETE', '/scim/token')
  },

  // ML platforms (API-6)
  listMlPlatforms(paging?: CursorPage): Promise<Page<MlPlatform>> {
    return request<Page<MlPlatform>>('GET', '/ml-platforms', undefined, { query: paging })
  },
  createMlPlatform(payload: MlPlatformCreate): Promise<MlPlatform> {
    return request<MlPlatform>('POST', '/ml-platforms', payload)
  },
  deleteMlPlatform(id: string): Promise<void> {
    return request<void>('DELETE', `/ml-platforms/${id}`)
  },
  checkMlPlatform(id: string): Promise<MlPlatformCheck> {
    return request<MlPlatformCheck>('POST', `/ml-platforms/${id}/check`)
  },
  publishSnapshot(
    projectId: string,
    snapshotId: string,
    payload: SnapshotPublishRequest,
  ): Promise<SnapshotPublishResult> {
    return request<SnapshotPublishResult>(
      'POST',
      `/projects/${projectId}/snapshots/${snapshotId}/mlflow`,
      payload,
    )
  },
  importModelVersion(modelId: string, payload: ModelVersionImport): Promise<ModelVersion> {
    return request<ModelVersion>('POST', `/models/${modelId}/versions/import`, payload)
  },

  // Models
  listModels(paging?: CursorPage): Promise<Page<Model>> {
    return request<Page<Model>>('GET', '/models', undefined, { query: paging })
  },
  createModel(payload: ModelCreate, idempotencyKey?: string): Promise<Model> {
    return request<Model>('POST', '/models', payload, { idempotencyKey })
  },
  updateModel(id: string, payload: ModelUpdate): Promise<Model> {
    return request<Model>('PATCH', `/models/${id}`, payload)
  },
  deleteModel(id: string): Promise<void> {
    return request<void>('DELETE', `/models/${id}`)
  },
  /** One click or box → a polygon from a segment model (ML-7). Nothing is stored. */
  interactiveSegment(itemId: string, payload: InteractiveRequest): Promise<InteractiveResult> {
    return request<InteractiveResult>('POST', `/items/${itemId}/interactive`, payload)
  },

  ocrPage(itemId: string, payload: OcrRequest): Promise<OcrResult> {
    return request<OcrResult>('POST', `/items/${itemId}/ocr`, payload)
  },
  /** `GET {endpoint_url}/info` with the model's credentials (BYOM-3). */
  checkModel(id: string): Promise<ModelCheck> {
    return request<ModelCheck>('POST', `/models/${id}/check`)
  },
  createModelVersion(modelId: string, payload: ModelVersionCreate): Promise<ModelVersion> {
    return request<ModelVersion>('POST', `/models/${modelId}/versions`, payload)
  },
  getModelFamily(modelId: string): Promise<ModelFamily> {
    return request<ModelFamily>('GET', `/models/${modelId}/family`)
  },

  listModelVersions(modelId: string, paging?: CursorPage): Promise<Page<ModelVersion>> {
    return request<Page<ModelVersion>>('GET', `/models/${modelId}/versions`, undefined, {
      query: paging,
    })
  },
  /** How much humans corrected a version's pre-labels (ML-5). */
  getCorrectionMetrics(
    modelId: string,
    versionId: string,
    projectId?: string,
  ): Promise<CorrectionMetrics> {
    return request<CorrectionMetrics>(
      'GET',
      `/models/${modelId}/versions/${versionId}/metrics`,
      undefined,
      { query: { project_id: projectId } },
    )
  },

  // Pre-labelling
  prelabelProject(
    projectId: string,
    payload: PrelabelRequest,
    idempotencyKey?: string,
  ): Promise<Job> {
    return request<Job>('POST', `/projects/${projectId}/prelabel`, payload, {
      idempotencyKey,
    })
  },

  // Comments (WF-5)
  /** Thread on an item, oldest first — a plain array, not paginated. */
  listComments(itemId: string): Promise<Comment[]> {
    return request<Comment[]>('GET', `/items/${itemId}/comments`)
  },
  createComment(itemId: string, payload: CommentCreate, idempotencyKey?: string): Promise<Comment> {
    return request<Comment>('POST', `/items/${itemId}/comments`, payload, {
      idempotencyKey,
    })
  },
  resolveComment(id: string, resolved: boolean): Promise<Comment> {
    return request<Comment>('POST', `/comments/${id}/resolve`, { resolved })
  },

  // Notifications (WF-5)
  listNotifications(filters?: CursorPage & { unread?: boolean }): Promise<Page<Notification>> {
    return request<Page<Notification>>('GET', '/notifications', undefined, {
      query: filters,
    })
  },
  unreadCount(): Promise<{ count: number }> {
    return request<{ count: number }>('GET', '/notifications/unread-count')
  },
  markNotificationRead(id: string): Promise<Notification> {
    return request<Notification>('POST', `/notifications/${id}/read`)
  },
  markAllNotificationsRead(): Promise<void> {
    return request<void>('POST', '/notifications/read-all')
  },

  // API keys (AUTH-4)
  listApiKeys(userId?: string): Promise<ApiKey[]> {
    return request<ApiKey[]>('GET', '/api-keys', undefined, {
      query: userId ? { user_id: userId } : undefined,
    })
  },
  /** The response's `token` is shown once; only metadata is readable afterwards. */
  createApiKey(payload: ApiKeyCreate): Promise<ApiKeyCreated> {
    return request<ApiKeyCreated>('POST', '/api-keys', payload)
  },
  revokeApiKey(id: string): Promise<void> {
    return request<void>('DELETE', `/api-keys/${id}`)
  },
  // Service accounts (AUTH-4) — superuser only
  listServiceAccounts(): Promise<User[]> {
    return request<User[]>('GET', '/service-accounts')
  },
  createServiceAccount(payload: ServiceAccountCreate): Promise<User> {
    return request<User>('POST', '/service-accounts', payload)
  },
  /** Deactivates the account and revokes all its keys. */
  deleteServiceAccount(id: string): Promise<void> {
    return request<void>('DELETE', `/service-accounts/${id}`)
  },
}
