/**
 * TanStack Query v5 hooks wrapping the typed API client (see client.ts).
 * Query keys are centralised in `queryKeys` so invalidation stays consistent.
 */
import { useCallback, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { UseQueryOptions } from '@tanstack/react-query'
import { api, type CursorPage, type ListItemsFilters, type ListTasksFilters } from './client'
import type {
  ConsensusResolve,
  GoldTasksRequest,
  SplitRequest,
  ApiKeyCreate,
  BulkRequest,
  ConnectorCreate,
  ConnectorUpdate,
  ImportUploadOptions,
  InteractiveRequest,
  OcrRequest,
  Job,
  LabelSchemaDefinition,
  LicenseKeyUpdate,
  LoginRequest,
  MemberCreate,
  ModelCreate,
  ModelUpdate,
  ModelVersionCreate,
  MlPlatformCreate,
  ModelVersionImport,
  SnapshotPublishRequest,
  Notification,
  Page,
  PrelabelRequest,
  Project,
  ProjectCreate,
  ProjectRole,
  ProjectUpdate,
  RetrainRequest,
  EraseRequest,
  ServiceAccountCreate,
  SnapshotCreate,
  Task,
  TaskUpdate,
  TileKey,
  WebhookCreate,
  WebhookUpdate,
  UserPreferencesUpdate,
} from './types'

export const queryKeys = {
  authProviders: ['auth', 'providers'] as const,
  apiKeys: (userId?: string) => ['api-keys', userId ?? 'me'] as const,
  serviceAccounts: () => ['service-accounts'] as const,
  users: (q = '') => ['users', q] as const,
  scimToken: () => ['scim-token'] as const,
  projects: (paging?: CursorPage) => ['projects', paging ?? {}] as const,
  project: (id: string) => ['projects', id] as const,
  items: (projectId: string, filters?: ListItemsFilters) =>
    ['projects', projectId, 'items', filters ?? {}] as const,
  item: (id: string) => ['items', id] as const,
  tasks: (projectId: string, filters?: ListTasksFilters) =>
    ['projects', projectId, 'tasks', filters ?? {}] as const,
  annotations: (itemId: string) => ['items', itemId, 'annotations'] as const,
  schemas: (projectId: string) => ['projects', projectId, 'schemas'] as const,
  members: (projectId: string) => ['projects', projectId, 'members'] as const,
  job: (id: string) => ['jobs', id] as const,
  jobs: (projectId: string, filters?: CursorPage) =>
    ['projects', projectId, 'jobs', filters ?? {}] as const,
  snapshots: (projectId: string, paging?: CursorPage) =>
    ['projects', projectId, 'snapshots', paging ?? {}] as const,
  snapshotDiff: (projectId: string, baseId: string, targetId: string) =>
    ['projects', projectId, 'snapshots', 'diff', baseId, targetId] as const,
  snapshotLineage: (projectId: string, snapshotId: string) =>
    ['projects', projectId, 'snapshots', snapshotId, 'lineage'] as const,
  stats: (projectId: string, days?: number) =>
    ['projects', projectId, 'stats', days ?? 14] as const,
  connectors: () => ['connectors'] as const,
  models: () => ['models'] as const,
  mlPlatforms: () => ['ml-platforms'] as const,
  correctionMetrics: (modelId: string, versionId: string, projectId?: string) =>
    ['models', modelId, 'versions', versionId, 'metrics', projectId ?? ''] as const,
  modelVersions: (modelId: string) => ['models', modelId, 'versions'] as const,
  // Its own root: a family spans models, so adding any version invalidates all of them.
  modelFamily: (modelId: string) => ['modelFamily', modelId] as const,
  webhooks: (projectId?: string) => ['webhooks', projectId ?? ''] as const,
  license: () => ['license'] as const,
  licenseRefresh: () => ['license', 'refresh'] as const,
  telemetryPreview: () => ['license', 'telemetry'] as const,
  usageNotices: () => ['license', 'usage-notices'] as const,
  seatReport: (start: string, end: string) => ['license', 'seat-report', start, end] as const,
  webhookDeliveries: (webhookId: string) => ['webhooks', 'deliveries', webhookId] as const,
  me: () => ['auth', 'me'] as const,
  mfa: () => ['auth', 'mfa'] as const,
  itemText: (url: string) => ['media-text', url] as const,
  consensus: (itemId: string) => ['items', itemId, 'consensus'] as const,
  agreement: (projectId: string) => ['projects', projectId, 'agreement'] as const,
  annotatorQuality: (projectId: string) =>
    ['projects', projectId, 'quality', 'annotators'] as const,
  comments: (itemId: string) => ['items', itemId, 'comments'] as const,
  notifications: (filters?: Parameters<typeof api.listNotifications>[0]) =>
    ['notifications', 'list', filters ?? {}] as const,
  unreadCount: () => ['notifications', 'unread-count'] as const,
}

// ---------------------------------------------------------------------------
// Projects
// ---------------------------------------------------------------------------

export function useAuthProviders() {
  return useQuery({
    queryKey: queryKeys.authProviders,
    queryFn: () => api.authProviders(),
    // Static per deployment; no point refetching on every login-page mount.
    staleTime: Infinity,
  })
}

export function useProjects(paging?: CursorPage) {
  return useQuery({
    queryKey: queryKeys.projects(paging),
    queryFn: () => api.listProjects(paging),
  })
}

export function useProject(id: string | undefined) {
  return useQuery({
    queryKey: queryKeys.project(id ?? ''),
    queryFn: () => api.getProject(id as string),
    enabled: Boolean(id),
  })
}

export function useCreateProject() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: ProjectCreate) => api.createProject(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['projects'] })
    },
  })
}

export function useUpdateProject(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: ProjectUpdate) => api.updateProject(projectId, payload),
    onSuccess: (project: Project) => {
      queryClient.setQueryData(queryKeys.project(projectId), project)
      void queryClient.invalidateQueries({ queryKey: ['projects'] })
    },
  })
}

export function useDeleteProject() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (projectId: string) => api.deleteProject(projectId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['projects'] })
    },
  })
}

// ---------------------------------------------------------------------------
// Project members (SEC-3)
// ---------------------------------------------------------------------------

export function useMembers(projectId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.members(projectId ?? ''),
    queryFn: () => api.listMembers(projectId as string),
    enabled: Boolean(projectId),
  })
}

export function useAddMember(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: MemberCreate) => api.addMember(projectId, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.members(projectId) })
    },
  })
}

export function useUpdateMemberFolders(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ userId, pathPrefixes }: { userId: string; pathPrefixes: string[] | null }) =>
      api.updateMemberFolders(projectId, userId, pathPrefixes),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.members(projectId) })
    },
  })
}

export function useUpdateMember(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: ProjectRole }) =>
      api.updateMember(projectId, userId, role),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.members(projectId) })
    },
  })
}

export function useRemoveMember(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (userId: string) => api.removeMember(projectId, userId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.members(projectId) })
    },
  })
}

// ---------------------------------------------------------------------------
// Items
// ---------------------------------------------------------------------------

export function useItems(projectId: string | undefined, filters?: ListItemsFilters) {
  return useQuery({
    queryKey: queryKeys.items(projectId ?? '', filters),
    queryFn: () => api.listItems(projectId as string, filters),
    enabled: Boolean(projectId),
  })
}

export function useItem(id: string | undefined) {
  return useQuery({
    queryKey: queryKeys.item(id ?? ''),
    queryFn: () => api.getItem(id as string),
    enabled: Boolean(id),
  })
}

/**
 * A stable `signTiles` callback for one item's Deep Zoom pyramid (IMG-1),
 * for the tiled image layer to call directly — signing is per-viewport, not
 * a cached resource, so this wraps the client call rather than `useQuery`.
 */
export function useSignTiles(itemId: string | undefined) {
  return useCallback(
    (tiles: TileKey[]) => api.signTiles(itemId as string, tiles).then((res) => res.urls),
    [itemId],
  )
}

/** A text item's content, read from its signed media URL (TOOL). */
export function useItemText(mediaUrl: string | null | undefined) {
  return useQuery({
    queryKey: queryKeys.itemText(mediaUrl ?? ''),
    queryFn: ({ signal }) => api.fetchMediaText(mediaUrl as string, signal),
    enabled: Boolean(mediaUrl),
    // A signed URL expires; the text behind a given URL never changes.
    staleTime: Infinity,
    retry: false,
  })
}

/** The item's companion views (§5 multimodal); only fetched when it has some. */
export function useItemViews(itemId: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: ['item-views', itemId ?? ''] as const,
    queryFn: () => api.getItemViews(itemId as string),
    enabled: Boolean(itemId) && enabled,
    // Signed URLs expire; refetch well before APP_SIGNED_URL_TTL.
    staleTime: 5 * 60_000,
  })
}

/** An item's media bytes (audio waveforms, §5); `null` disables the fetch. */
export function useItemBytes(mediaUrl: string | null | undefined) {
  return useQuery({
    queryKey: ['item-bytes', mediaUrl ?? ''] as const,
    queryFn: ({ signal }) => api.fetchMediaBytes(mediaUrl as string, signal),
    enabled: Boolean(mediaUrl),
    staleTime: Infinity,
    gcTime: 60_000,
    retry: false,
  })
}

// ---------------------------------------------------------------------------
// Quality control (QA-1 … QA-4) and region split (IMG-6)
// ---------------------------------------------------------------------------

export function useSplitItem(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ itemId, body }: { itemId: string; body: SplitRequest }) =>
      api.splitItem(itemId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['projects', projectId] })
    },
  })
}

export function useConsensus(itemId: string | undefined, enabled = true) {
  return useQuery({
    queryKey: queryKeys.consensus(itemId ?? ''),
    queryFn: () => api.getConsensus(itemId as string),
    enabled: Boolean(itemId) && enabled,
    retry: false,
  })
}

export function useResolveConsensus(itemId: string, projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: ConsensusResolve) => api.resolveConsensus(itemId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['items', itemId] })
      void queryClient.invalidateQueries({ queryKey: ['projects', projectId] })
    },
  })
}

export function useSetGold(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ itemId, annotationId }: { itemId: string; annotationId: string | null }) =>
      annotationId === null ? api.clearGold(itemId) : api.setGold(itemId, annotationId),
    onSuccess: (item) => {
      queryClient.setQueryData(queryKeys.item(item.id), item)
      void queryClient.invalidateQueries({ queryKey: ['projects', projectId] })
    },
  })
}

export function useOpenGoldTasks(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body?: GoldTasksRequest) => api.openGoldTasks(projectId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'tasks'] })
    },
  })
}

export function useAgreement(projectId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.agreement(projectId ?? ''),
    queryFn: () => api.getAgreement(projectId as string),
    enabled: Boolean(projectId),
  })
}

export function useAnnotatorQuality(projectId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.annotatorQuality(projectId ?? ''),
    queryFn: () => api.getAnnotatorQuality(projectId as string),
    enabled: Boolean(projectId),
  })
}

export function useScanProject() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (projectId: string) => api.scanProject(projectId),
    onSuccess: (_job, projectId) => {
      void queryClient.invalidateQueries({
        queryKey: ['projects', projectId, 'items'],
      })
    },
  })
}

/** Queue PDF text extraction for the project's text-mode PDFs (PDF text mode). */
export function useExtractPdfText(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (force: boolean) => api.extractPdfText(projectId, force),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'jobs'] })
      void queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'items'] })
    },
  })
}

/** Queue a derived-data cache rebuild (SRC-6). */
export function useRebuildCache(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (purge: boolean) => api.rebuildCache(projectId, purge),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'jobs'] })
      void queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'items'] })
    },
  })
}

/**
 * Upload files to the project's source connector, then queue a scan so they
 * become items. Mints URLs in one request, PUTs with bounded concurrency, and
 * reports progress through `onProgress` as each file lands.
 */
export function useUploadFiles(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({
      files,
      onProgress,
    }: {
      files: UploadEntry[]
      onProgress?: (done: number, total: number) => void
    }): Promise<UploadOutcome> => {
      const { uploads } = await api.createUploadUrls(projectId, {
        files: files.map((entry) => ({
          path: entry.path,
          content_type: entry.file.type || null,
          size_bytes: entry.file.size,
        })),
      })
      const failed: string[] = []
      let done = 0
      let next = 0
      const worker = async (): Promise<void> => {
        while (next < files.length) {
          const entry = files[next]
          const target = uploads[next]
          next += 1
          try {
            await api.putToSignedUrl(target, entry.file)
          } catch {
            failed.push(entry.path)
          }
          done += 1
          onProgress?.(done, files.length)
        }
      }
      await Promise.all(Array.from({ length: UPLOAD_CONCURRENCY }, worker))
      const uploaded = files.length - failed.length
      const job = uploaded > 0 ? await api.scanProject(projectId) : null
      return { uploaded, failed, job, paths: uploads.map((target) => target.path) }
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.jobs(projectId) })
      void queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'items'] })
    },
  })
}

/** How many PUTs run at once; browsers cap per-host connections at ~6 anyway. */
const UPLOAD_CONCURRENCY = 4

export interface UploadEntry {
  /** Relative path (from `webkitRelativePath` or the file name). */
  path: string
  file: File
}

export interface UploadOutcome {
  uploaded: number
  failed: string[]
  /** The scan queued after the uploads, or `null` when nothing landed. */
  job: Job | null
  paths: string[]
}

export function useCreateThumbnails(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload?: { force?: boolean }) => api.createThumbnails(projectId, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'jobs'] })
    },
  })
}

// ---------------------------------------------------------------------------
// Tasks
// ---------------------------------------------------------------------------

export function useTasks(projectId: string | undefined, filters?: ListTasksFilters) {
  return useQuery({
    queryKey: queryKeys.tasks(projectId ?? '', filters),
    queryFn: () => api.listTasks(projectId as string, filters),
    enabled: Boolean(projectId),
  })
}

export function useBulkItems(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: BulkRequest) => api.bulkItems(projectId, body),
    onSuccess: () => {
      // Items, tasks and dashboard numbers all move under a bulk action.
      void queryClient.invalidateQueries({ queryKey: ['projects', projectId] })
    },
  })
}

export function useUpdateTask(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: TaskUpdate }) => api.updateTask(id, patch),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'tasks'] })
    },
  })
}

export function useNextTask() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: { project_id: string; type?: Task['type'] }) => api.nextTask(payload),
    onSuccess: (_task, payload) => {
      void queryClient.invalidateQueries({
        queryKey: ['projects', payload.project_id, 'tasks'],
      })
    },
  })
}

export function useReleaseTask() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.releaseTask(id),
    onSuccess: (task) => {
      void queryClient.invalidateQueries({
        queryKey: ['projects', task.project_id, 'tasks'],
      })
    },
  })
}

export function useExtendTask() {
  return useMutation({
    mutationFn: (id: string) => api.extendTask(id),
  })
}

// ---------------------------------------------------------------------------
// Annotations
// ---------------------------------------------------------------------------

export function useAnnotations(itemId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.annotations(itemId ?? ''),
    queryFn: () => api.listAnnotations(itemId as string),
    enabled: Boolean(itemId),
  })
}

export function useCreateAnnotation(itemId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: Parameters<typeof api.createAnnotation>[1]) =>
      api.createAnnotation(itemId, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.annotations(itemId),
      })
      void queryClient.invalidateQueries({ queryKey: queryKeys.item(itemId) })
    },
  })
}

export function useSubmitAnnotation(itemId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (annotationId: string) => api.submitAnnotation(annotationId),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.annotations(itemId),
      })
      void queryClient.invalidateQueries({ queryKey: queryKeys.item(itemId) })
    },
  })
}

export function useReviewAnnotation(itemId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, ...verdict }: { id: string } & Parameters<typeof api.reviewAnnotation>[1]) =>
      api.reviewAnnotation(id, verdict),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.annotations(itemId),
      })
      void queryClient.invalidateQueries({ queryKey: queryKeys.item(itemId) })
    },
  })
}

// ---------------------------------------------------------------------------
// Schemas
// ---------------------------------------------------------------------------

export function useSchemas(projectId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.schemas(projectId ?? ''),
    queryFn: () => api.listSchemas(projectId as string),
    enabled: Boolean(projectId),
  })
}

export function useCreateSchemaVersion(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (definition: LabelSchemaDefinition) =>
      api.createSchemaVersion(projectId, definition),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.schemas(projectId) })
      // The first version also sets `project.label_schema_id`.
      void queryClient.invalidateQueries({ queryKey: queryKeys.project(projectId) })
    },
  })
}

// ---------------------------------------------------------------------------
// Exports / jobs
// ---------------------------------------------------------------------------

export function useProjectStats(projectId: string | undefined, days?: number) {
  return useQuery({
    queryKey: queryKeys.stats(projectId ?? '', days),
    queryFn: () =>
      api.getProjectStats(projectId as string, days === undefined ? undefined : { days }),
    enabled: Boolean(projectId),
  })
}

export function useCreateExport(projectId: string) {
  return useMutation({
    mutationFn: (payload?: Record<string, unknown>) => api.createExport(projectId, payload),
  })
}

// ---------------------------------------------------------------------------
// Snapshots
// ---------------------------------------------------------------------------

export function useSnapshots(projectId: string | undefined, paging?: CursorPage) {
  return useQuery({
    queryKey: queryKeys.snapshots(projectId ?? '', paging),
    queryFn: () => api.listSnapshots(projectId as string, paging),
    enabled: Boolean(projectId),
  })
}

export function useCreateSnapshot(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: SnapshotCreate) => api.createSnapshot(projectId, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'jobs'] })
    },
  })
}

export function useSnapshotDiff(
  projectId: string | undefined,
  baseId: string | undefined,
  targetId: string | undefined,
) {
  return useQuery({
    queryKey: queryKeys.snapshotDiff(projectId ?? '', baseId ?? '', targetId ?? ''),
    queryFn: () => api.diffSnapshots(projectId as string, baseId as string, targetId as string),
    enabled: Boolean(projectId && baseId && targetId && baseId !== targetId),
  })
}

export function useSnapshotLineage(projectId: string, snapshotId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.snapshotLineage(projectId, snapshotId ?? ''),
    queryFn: () => api.snapshotLineage(projectId, snapshotId as string),
    enabled: Boolean(snapshotId),
  })
}

/**
 * Uploads an import file and tracks upload progress (a 0-1 fraction, `null`
 * when idle) alongside the usual mutation state.
 */
export function useUploadImport(projectId: string) {
  const queryClient = useQueryClient()
  const [progress, setProgress] = useState<number | null>(null)
  const mutation = useMutation({
    mutationFn: ({ file, options }: { file: File; options: ImportUploadOptions }) => {
      setProgress(0)
      return api.uploadImport(projectId, file, options, setProgress)
    },
    onSettled: () => {
      setProgress(null)
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.jobs(projectId),
      })
    },
  })
  return { ...mutation, progress }
}

export function useJobs(
  projectId: string | undefined,
  filters?: Parameters<typeof api.listJobs>[1],
  options?: Pick<UseQueryOptions<Page<Job>>, 'refetchInterval'>,
) {
  return useQuery({
    queryKey: queryKeys.jobs(projectId ?? '', filters),
    queryFn: () => api.listJobs(projectId as string, filters),
    enabled: Boolean(projectId),
    refetchInterval: options?.refetchInterval,
  })
}

/** Re-queue a failed or cancelled job; refreshes its project's job lists. */
export function useRetryJob() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (jobId: string) => api.retryJob(jobId),
    onSuccess: (job) => {
      queryClient.setQueryData(queryKeys.job(job.id), job)
      if (job.project_id) {
        void queryClient.invalidateQueries({ queryKey: ['projects', job.project_id, 'jobs'] })
      }
    },
  })
}

export function useDownloadExport() {
  return useMutation({
    mutationFn: (jobId: string) => api.downloadExport(jobId),
  })
}

export function useJob(
  id: string | undefined,
  options?: Pick<UseQueryOptions<Job>, 'refetchInterval'>,
) {
  return useQuery({
    queryKey: queryKeys.job(id ?? ''),
    queryFn: () => api.getJob(id as string),
    enabled: Boolean(id),
    refetchInterval: options?.refetchInterval,
  })
}

// ---------------------------------------------------------------------------
// Connectors
// ---------------------------------------------------------------------------

export function useConnectors() {
  return useQuery({
    queryKey: queryKeys.connectors(),
    queryFn: () => api.listConnectors(),
  })
}

export function useCheckConnector() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.checkConnector(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.connectors() })
    },
  })
}

export function useCreateConnector() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: ConnectorCreate) => api.createConnector(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.connectors() })
    },
  })
}

export function useUpdateConnector() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: ConnectorUpdate }) =>
      api.updateConnector(id, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.connectors() })
    },
  })
}

export function useMintConnectorEventToken() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.mintConnectorEventToken(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.connectors() })
    },
  })
}

export function useRevokeConnectorEventToken() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.revokeConnectorEventToken(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.connectors() })
    },
  })
}

export function useScimToken(enabled = true) {
  return useQuery({
    queryKey: queryKeys.scimToken(),
    queryFn: () => api.getScimToken(),
    enabled,
  })
}

export function useMintScimToken() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.mintScimToken(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.scimToken() })
    },
  })
}

export function useRevokeScimToken() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.revokeScimToken(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.scimToken() })
    },
  })
}

export function useDeleteConnector() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.deleteConnector(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.connectors() })
    },
  })
}

// ---------------------------------------------------------------------------
// Models / pre-labelling (ML-1, ML-2, BYOM-7)
// ---------------------------------------------------------------------------

export function useModels() {
  return useQuery({
    queryKey: queryKeys.models(),
    queryFn: () => api.listModels(),
  })
}

export function useModelVersions(modelId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.modelVersions(modelId ?? ''),
    queryFn: () => api.listModelVersions(modelId as string),
    enabled: Boolean(modelId),
  })
}

/** The derivation graph around a model (EXP-8), fetched when `enabled`. */
export function useModelFamily(modelId: string, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.modelFamily(modelId),
    queryFn: () => api.getModelFamily(modelId),
    enabled,
  })
}

export function useCorrectionMetrics(
  modelId: string | undefined,
  versionId: string | undefined,
  projectId?: string,
  enabled = true,
) {
  return useQuery({
    queryKey: queryKeys.correctionMetrics(modelId ?? '', versionId ?? '', projectId),
    queryFn: () => api.getCorrectionMetrics(modelId as string, versionId as string, projectId),
    enabled: enabled && Boolean(modelId && versionId),
  })
}

export function useCreateModel() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: ModelCreate) => api.createModel(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.models() })
    },
  })
}

export function useUpdateModel() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: ModelUpdate }) =>
      api.updateModel(id, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.models() })
    },
  })
}

export function useDeleteModel() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.deleteModel(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.models() })
    },
  })
}

export function useInteractiveSegment(itemId: string | undefined) {
  return useMutation({
    mutationFn: (payload: InteractiveRequest) => {
      if (!itemId) return Promise.reject(new Error('No item'))
      return api.interactiveSegment(itemId, payload)
    },
  })
}

/** Words of one scanned PDF page through an `ocr` model; nothing is cached or stored. */
export function useItemOcr(itemId: string | undefined) {
  return useMutation({
    mutationFn: (payload: OcrRequest) => {
      if (!itemId) return Promise.reject(new Error('No item'))
      return api.ocrPage(itemId, payload)
    },
  })
}

// ---------------------------------------------------------------------------
// ML platforms (API-6)
// ---------------------------------------------------------------------------

export function useMlPlatforms(enabled = true) {
  return useQuery({
    queryKey: queryKeys.mlPlatforms(),
    queryFn: () => api.listMlPlatforms({ limit: 100 }),
    enabled,
  })
}

export function useCreateMlPlatform() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: MlPlatformCreate) => api.createMlPlatform(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.mlPlatforms() })
    },
  })
}

export function useDeleteMlPlatform() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.deleteMlPlatform(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.mlPlatforms() })
    },
  })
}

export function useCheckMlPlatform() {
  return useMutation({
    mutationFn: (id: string) => api.checkMlPlatform(id),
  })
}

export function usePublishSnapshot(projectId: string) {
  return useMutation({
    mutationFn: ({
      snapshotId,
      payload,
    }: {
      snapshotId: string
      payload: SnapshotPublishRequest
    }) => api.publishSnapshot(projectId, snapshotId, payload),
  })
}

export function useImportModelVersion(modelId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: ModelVersionImport) => api.importModelVersion(modelId, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.modelVersions(modelId) })
      void queryClient.invalidateQueries({ queryKey: ['modelFamily'] })
    },
  })
}

export function useCheckModel() {
  return useMutation({
    mutationFn: (id: string) => api.checkModel(id),
  })
}

export function useCreateModelVersion(modelId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: ModelVersionCreate) => api.createModelVersion(modelId, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.modelVersions(modelId) })
      void queryClient.invalidateQueries({ queryKey: ['modelFamily'] })
    },
  })
}

export function usePrelabelProject(projectId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: PrelabelRequest) => api.prelabelProject(projectId, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ['projects', projectId, 'jobs'],
      })
      void queryClient.invalidateQueries({
        queryKey: ['projects', projectId, 'items'],
      })
    },
  })
}

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

/** The caller's MFA state (AUTH-2). */
export function useMfa() {
  return useQuery({ queryKey: queryKeys.mfa(), queryFn: () => api.getMfa() })
}

/** Start enrolment: a new pending seed. */
export function useSetupMfa() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.setupMfa(),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: queryKeys.mfa() }),
  })
}

/** Confirm the pending seed; resolves to the recovery codes. */
export function useEnableMfa() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (code: string) => api.enableMfa(code),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: queryKeys.mfa() }),
  })
}

export function useReplaceRecoveryCodes() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (code: string) => api.replaceRecoveryCodes(code),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: queryKeys.mfa() }),
  })
}

export function useDisableMfa() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (code: string) => api.disableMfa(code),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: queryKeys.mfa() }),
  })
}

export function useLogin() {
  return useMutation({
    mutationFn: (payload: LoginRequest) => api.login(payload),
  })
}

/** Fetch the caller's own personal data export (SEC-6). */
export function useExportPersonalData() {
  return useMutation({ mutationFn: (userId: string) => api.exportPersonalData(userId) })
}

/** The organisation's people, erased ones included (superuser only). */
export function useUsers(q: string, enabled = true) {
  return useQuery({ queryKey: queryKeys.users(q), queryFn: () => api.listUsers(q), enabled })
}

/** SEC-6 erasure: the row stays, pseudonymised; the directory refreshes. */
export function useEraseUser() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ userId, ...payload }: EraseRequest & { userId: string }) =>
      api.eraseUser(userId, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['users'] })
    },
  })
}

export function useMe(enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.me(),
    queryFn: () => api.me(),
    enabled,
  })
}

/** `PATCH /auth/me` (API-7): the caller's own preferences. */
export function useUpdateMe() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: UserPreferencesUpdate) => api.updateMe(body),
    onSuccess: (user) => {
      queryClient.setQueryData(queryKeys.me(), user)
    },
  })
}

// ---------------------------------------------------------------------------
// Comments (WF-5)
// ---------------------------------------------------------------------------

export function useComments(itemId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.comments(itemId ?? ''),
    queryFn: () => api.listComments(itemId as string),
    enabled: Boolean(itemId),
  })
}

export function useCreateComment(itemId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: Parameters<typeof api.createComment>[1]) =>
      api.createComment(itemId, payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.comments(itemId),
      })
    },
  })
}

export function useResolveComment(itemId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, resolved }: { id: string; resolved: boolean }) =>
      api.resolveComment(id, resolved),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.comments(itemId),
      })
    },
  })
}

// ---------------------------------------------------------------------------
// Notifications (WF-5)
// ---------------------------------------------------------------------------

export function useNotifications(
  filters?: Parameters<typeof api.listNotifications>[0],
  options?: Pick<UseQueryOptions<Page<Notification>>, 'refetchInterval'>,
) {
  return useQuery({
    queryKey: queryKeys.notifications(filters),
    queryFn: () => api.listNotifications(filters),
    refetchInterval: options?.refetchInterval,
  })
}

export function useUnreadCount(
  options?: Pick<UseQueryOptions<{ count: number }>, 'refetchInterval'>,
) {
  return useQuery({
    queryKey: queryKeys.unreadCount(),
    queryFn: () => api.unreadCount(),
    refetchInterval: options?.refetchInterval,
  })
}

export function useMarkNotificationRead() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.markNotificationRead(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['notifications'] })
    },
  })
}

export function useMarkAllRead() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.markAllNotificationsRead(),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['notifications'] })
    },
  })
}

// ---------------------------------------------------------------------------
// API keys (AUTH-4)
// ---------------------------------------------------------------------------

export function useApiKeys(userId?: string) {
  return useQuery({
    queryKey: queryKeys.apiKeys(userId),
    queryFn: () => api.listApiKeys(userId),
  })
}

export function useCreateApiKey() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: ApiKeyCreate) => api.createApiKey(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['api-keys'] })
    },
  })
}

export function useRevokeApiKey() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.revokeApiKey(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['api-keys'] })
    },
  })
}

/** The organisation's service accounts, active or not (superuser only). */
export function useServiceAccounts(enabled = true) {
  return useQuery({
    queryKey: queryKeys.serviceAccounts(),
    queryFn: () => api.listServiceAccounts(),
    enabled,
  })
}

export function useCreateServiceAccount() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (payload: ServiceAccountCreate) => api.createServiceAccount(payload),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.serviceAccounts() })
    },
  })
}

/** Deactivation also revokes every key the account holds, so both lists refresh. */
export function useDeleteServiceAccount() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.deleteServiceAccount(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.serviceAccounts() })
      void queryClient.invalidateQueries({ queryKey: ['api-keys'] })
    },
  })
}

// ---------------------------------------------------------------------------
// Webhooks (API-4) and retraining (ML-9)
// ---------------------------------------------------------------------------

export function useWebhooks(projectId?: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.webhooks(projectId),
    queryFn: () => api.listWebhooks(projectId, { limit: 50 }),
    enabled,
  })
}

export function useCreateWebhook(projectId?: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: WebhookCreate) => api.createWebhook(body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.webhooks(projectId) })
    },
  })
}

export function useUpdateWebhook(projectId?: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: WebhookUpdate }) => api.updateWebhook(id, body),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.webhooks(projectId) })
    },
  })
}

export function useDeleteWebhook(projectId?: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.deleteWebhook(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.webhooks(projectId) })
    },
  })
}

export function useTestWebhook() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.testWebhook(id),
    onSuccess: (delivery) => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.webhookDeliveries(delivery.webhook_id),
      })
    },
  })
}

export function useWebhookDeliveries(webhookId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.webhookDeliveries(webhookId ?? ''),
    queryFn: () => api.listWebhookDeliveries(webhookId as string, { limit: 20 }),
    enabled: Boolean(webhookId),
    // Deliveries move on the worker's 15 s tick; poll while the log is open.
    refetchInterval: 15000,
  })
}

export function useRequestRetrain(projectId: string) {
  return useMutation({
    mutationFn: (body: RetrainRequest) => api.requestRetrain(projectId, body),
  })
}

// ---------------------------------------------------------------------------
// Licence (LIC-24, LIC-26)
// ---------------------------------------------------------------------------

/** Superuser only: pass `enabled = false` for everyone else. */
export function useLicense(enabled = true) {
  return useQuery({
    queryKey: queryKeys.license(),
    queryFn: () => api.getLicense(),
    enabled,
  })
}

/** Superuser only (LIC-30), and a Business feature. Empty dates fall back to the last 365 days. */
export function useSeatReport(start: string, end: string, enabled = true) {
  return useQuery({
    queryKey: queryKeys.seatReport(start, end),
    queryFn: () => api.getSeatReport({ start: start || undefined, end: end || undefined }),
    enabled,
  })
}

/** Superuser only (LIC-27). */
export function useLicenseRefresh() {
  return useQuery({ queryKey: queryKeys.licenseRefresh(), queryFn: () => api.getLicenseRefresh() })
}

/** Ask the licence server for a renewed key now; a stored key changes the licence in force. */
export function useRefreshLicenseNow() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.refreshLicenseNow(),
    onSuccess: (status) => {
      queryClient.setQueryData(queryKeys.licenseRefresh(), status)
      void queryClient.invalidateQueries({ queryKey: queryKeys.license(), exact: true })
    },
  })
}

/** Superuser only: signs of seat sharing (LIC-31). */
export function useUsageNotices() {
  return useQuery({ queryKey: queryKeys.usageNotices(), queryFn: () => api.getUsageNotices() })
}

/** Superuser only: the heartbeat payload, sent or not (LIC-6, LIC-21). */
export function useTelemetryPreview() {
  return useQuery({
    queryKey: queryKeys.telemetryPreview(),
    queryFn: () => api.getTelemetryPreview(),
  })
}

/** Start the 30-day Business trial (LIC-34); the trial key becomes the licence in force. */
export function useStartTrial() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.startTrial(),
    onSuccess: (licence) => queryClient.setQueryData(queryKeys.license(), licence),
  })
}

export function useInstallLicense() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: LicenseKeyUpdate) => api.installLicense(body),
    onSuccess: (licence) => queryClient.setQueryData(queryKeys.license(), licence),
  })
}

export function useRemoveLicense() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: () => api.removeLicense(),
    onSuccess: (licence) => queryClient.setQueryData(queryKeys.license(), licence),
  })
}
