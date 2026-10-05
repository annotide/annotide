import { useEffect, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import type { TFunction } from 'i18next'
import { formatDateTime } from '@/i18n'
import {
  useCreateExport,
  useCreateThumbnails,
  useDownloadExport,
  useItems,
  useJobs,
  useMembers,
  useModelVersions,
  useModels,
  usePrelabelProject,
  useProject,
} from '@/api/queries'
import type { Item, ItemStatus, Job, MediaType } from '@/api/types'
import { ApiError } from '@/api/client'
import { Spinner } from '@/components/Spinner'
import { ErrorState } from '@/components/ErrorState'
import { EmptyState } from '@/components/EmptyState'
import { Button } from '@/components/Button'
import { JobRetryButton } from '@/components/JobRetryButton'
import { ImportPanel } from '@/components/ImportPanel'
import { UploadPanel } from '@/components/UploadPanel'
import { TasksPanel } from '@/components/TasksPanel'
import { SnapshotsPanel } from '@/components/SnapshotsPanel'
import { QualityPanel } from '@/components/QualityPanel'
import { ScanPanel } from '@/components/ScanPanel'
import { TabPanel, Tabs } from '@/components/Tabs'
import { SplitControl } from '@/components/SplitControl'
import { BulkActionsBar } from '@/components/BulkActionsBar'
import { useAuthStore } from '@/lib/store'

const MEDIA_ICONS: Record<MediaType, string> = {
  image: '🖼️',
  pdf: '📄',
  text: '📝',
  video: '🎞️',
  audio: '🔊',
  llm: '💬',
  timeseries: '📈',
}

type ExportFormat = 'coco' | 'yolo' | 'yolo_pose' | 'native' | 'spacy' | 'conll'

/** Tags set by the bulk `tag` action live in `item.meta.tags` (WF-8). */
function itemTags(item: Item): string[] {
  const tags = item.meta.tags
  return Array.isArray(tags) ? tags.filter((t): t is string => typeof t === 'string') : []
}

const EXPORT_FORMAT_OPTIONS: Array<{ value: ExportFormat; label: string }> = [
  { value: 'coco', label: 'COCO' },
  { value: 'yolo', label: 'YOLO' },
  { value: 'yolo_pose', label: 'YOLO pose (keypoints)' },
  { value: 'native', label: 'Native' },
  { value: 'spacy', label: 'spaCy JSONL (text)' },
  { value: 'conll', label: 'CoNLL IOB2 (text)' },
]

const LINK_BUTTON_CLASS =
  'inline-flex items-center justify-center gap-2 rounded-md px-4 py-2 text-sm font-medium ' +
  'transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 ' +
  'focus-visible:outline-accent'

function isInFlight(job: Job): boolean {
  return job.status === 'queued' || job.status === 'running'
}

/** Summarises a succeeded `prelabel` job's `result` (ML-2). Read defensively:
 * the backend is developed concurrently against this contract. */
function prelabelSummary(job: Job, t: TFunction<'projects'>): string {
  const result = job.result
  const predicted = Number(result?.predicted ?? 0)
  const selected = Number(result?.selected ?? 0)
  const empty = Number(result?.empty ?? 0)
  const skippedHuman = Number(result?.skipped_human ?? 0)
  const errors = Number(result?.errors ?? 0)
  return t('detail.prelabel.summary', { predicted, selected, empty, skippedHuman, errors })
}

const STATUS_VALUES: Array<ItemStatus | 'all'> = [
  'all',
  'new',
  'prelabeled',
  'annotating',
  'submitted',
  'in_review',
  'approved',
  'rejected',
  'skipped',
]

function statusOptionLabel(value: ItemStatus | 'all', t: TFunction<'projects'>): string {
  return value === 'all' ? t('status.all') : t(`status.${value}`)
}

const STATUS_BADGE_CLASSES: Record<ItemStatus, string> = {
  new: 'bg-slate-500/20 text-slate-800 dark:text-slate-300',
  prelabeled: 'bg-cyan-500/20 text-cyan-800 dark:text-cyan-300',
  annotating: 'bg-amber-500/20 text-amber-800 dark:text-amber-300',
  submitted: 'bg-blue-500/20 text-blue-800 dark:text-blue-300',
  in_review: 'bg-purple-500/20 text-purple-800 dark:text-purple-300',
  approved: 'bg-green-500/20 text-green-800 dark:text-green-300',
  rejected: 'bg-red-500/20 text-red-800 dark:text-red-300',
  skipped: 'bg-gray-500/20 text-gray-800 dark:text-gray-300',
}

function StatusBadge({ status }: { status: ItemStatus }): JSX.Element {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_BADGE_CLASSES[status]}`}
    >
      {status.replace('_', ' ')}
    </span>
  )
}

const PAGE_LIMIT = 30

// The items grid is what people come for; the data and job panels sit in
// their own tabs instead of stacking above it. `?tab=` makes each linkable.
const PROJECT_TABS = ['items', 'data', 'exports', 'tasks', 'prelabel'] as const
type ProjectTab = (typeof PROJECT_TABS)[number]

function isProjectTab(value: string | null): value is ProjectTab {
  return PROJECT_TABS.some((tab) => tab === value)
}

export function ProjectDetailPage(): JSX.Element {
  const { t } = useTranslation(['projects', 'common'])
  const { projectId } = useParams<{ projectId: string }>()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const tabParam = searchParams.get('tab')
  const tab: ProjectTab = isProjectTab(tabParam) ? tabParam : 'items'
  const setTab = (next: ProjectTab): void =>
    setSearchParams(next === 'items' ? {} : { tab: next }, { replace: true })
  const [statusFilter, setStatusFilter] = useState<ItemStatus | 'all'>('all')
  // Tag filter (WF-8): typed into `tagInput`, applied on Enter / blur.
  const [tagInput, setTagInput] = useState('')
  const [tagFilter, setTagFilter] = useState('')
  const [cursor, setCursor] = useState<string | undefined>(undefined)
  const [items, setItems] = useState<Item[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [exportFormat, setExportFormat] = useState<ExportFormat>('coco')
  const [prelabelModelId, setPrelabelModelId] = useState<string | undefined>(undefined)
  const [prelabelVersionId, setPrelabelVersionId] = useState<string | undefined>(undefined)
  const [prelabelLimit, setPrelabelLimit] = useState<string>('')
  const [prelabelConfidence, setPrelabelConfidence] = useState<number>(0)
  const [prelabelPrioritize, setPrelabelPrioritize] = useState(false)

  const project = useProject(projectId)
  const membersQuery = useMembers(projectId)
  const currentUser = useAuthStore((s) => s.user)
  const myRole = membersQuery.data?.find((m) => m.user_id === currentUser?.id)?.role
  // Bulk actions (WF-8) are owner / reviewer work; the server enforces it too.
  const canBulk = Boolean(currentUser?.is_superuser) || myRole === 'owner' || myRole === 'reviewer'
  const itemsQuery = useItems(projectId, {
    limit: PAGE_LIMIT,
    cursor,
    status: statusFilter === 'all' ? undefined : statusFilter,
    tag: tagFilter || undefined,
  })

  const createExport = useCreateExport(projectId ?? '')
  const createThumbnails = useCreateThumbnails(projectId ?? '')
  const downloadExport = useDownloadExport()
  const jobsQuery = useJobs(
    projectId,
    { type: 'export', limit: 10 },
    {
      refetchInterval: (query) => {
        const jobs = query.state.data?.items ?? []
        return jobs.some(isInFlight) ? 3000 : false
      },
    },
  )

  const modelsQuery = useModels()
  const modelVersionsQuery = useModelVersions(prelabelModelId)
  const prelabelMutation = usePrelabelProject(projectId ?? '')
  const prelabelJobsQuery = useJobs(
    projectId,
    { type: 'prelabel', limit: 10 },
    {
      refetchInterval: (query) => {
        const jobs = query.state.data?.items ?? []
        return jobs.some(isInFlight) ? 3000 : false
      },
    },
  )

  // An external producer (API-8) has no endpoint to run a job against.
  const models = (modelsQuery.data?.items ?? []).filter((model) => model.endpoint_url !== null)
  const modelVersions = modelVersionsQuery.data?.items ?? []

  // Default to the first model once the list loads.
  useEffect(() => {
    if (prelabelModelId) return
    const first = models[0]
    if (first) setPrelabelModelId(first.id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelsQuery.data])

  // A different model invalidates any previously selected version.
  useEffect(() => {
    setPrelabelVersionId(undefined)
  }, [prelabelModelId])

  // Default to the highest version once the list loads.
  useEffect(() => {
    if (prelabelVersionId) return
    if (modelVersions.length === 0) return
    const highest = modelVersions.reduce((max, v) => (v.version > max.version ? v : max))
    setPrelabelVersionId(highest.id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [modelVersionsQuery.data])

  // Reset the accumulated list whenever the project or filter changes.
  useEffect(() => {
    setItems([])
    setCursor(undefined)
    setSelected(new Set())
  }, [projectId, statusFilter, tagFilter])

  function toggleSelected(id: string): void {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const allSelected = items.length > 0 && items.every((item) => selected.has(item.id))

  // Append each fetched page to the accumulated list (UX-6: cursor "load more").
  useEffect(() => {
    if (!itemsQuery.data) return
    setItems((prev) => (cursor ? [...prev, ...itemsQuery.data.items] : itemsQuery.data.items))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [itemsQuery.data])

  if (!projectId) {
    return <ErrorState title={t('errors.missingProjectId')} />
  }

  const tabs = PROJECT_TABS.map((id) => ({ id, label: t(`detail.tabs.${id}`) }))
  const filtered = statusFilter !== 'all' || tagFilter !== ''

  return (
    <div className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-6">
      {project.isLoading && (
        <div className="flex justify-center py-16">
          <Spinner label={t('detail.loading')} />
        </div>
      )}

      {project.isError && (
        <ErrorState
          title={t('detail.loadError')}
          message={
            project.error instanceof ApiError
              ? (project.error.detail ?? project.error.title)
              : t('errors.unknown')
          }
          onRetry={() => void project.refetch()}
        />
      )}

      {project.data && (
        <header className="mb-6">
          <h1 className="text-xl font-semibold text-ink">{project.data.name}</h1>
          {project.data.description && (
            <p className="mt-1 text-sm text-muted">{project.data.description}</p>
          )}
        </header>
      )}

      {projectId && (
        <div className="mb-8 flex flex-wrap gap-3">
          <Link
            to={`/projects/${projectId}/annotate`}
            className={`${LINK_BUTTON_CLASS} bg-accent-fill text-white hover:opacity-90`}
          >
            {t('nav.annotate')}
          </Link>
          <Link
            to={`/projects/${projectId}/review`}
            className={`${LINK_BUTTON_CLASS} border border-line bg-surface text-ink hover:bg-line/40`}
          >
            {t('nav.review')}
          </Link>
          <Link
            to={`/projects/${projectId}/dashboard`}
            className={`${LINK_BUTTON_CLASS} border border-line bg-surface text-ink hover:bg-line/40`}
          >
            {t('nav.dashboard')}
          </Link>
          <Link
            to={`/projects/${projectId}/settings`}
            className={`${LINK_BUTTON_CLASS} border border-line bg-surface text-ink hover:bg-line/40`}
          >
            {t('nav.settings')}
          </Link>
        </div>
      )}

      <Tabs
        label={t('detail.tabs.label')}
        idPrefix="project"
        tabs={tabs}
        value={tab}
        onChange={setTab}
      />

      <TabPanel idPrefix="project" id="items" active={tab === 'items'}>
        <div className="mb-4 flex flex-wrap items-center gap-3">
          <label htmlFor="status-filter" className="text-sm font-medium text-ink">
            {t('detail.statusFilter')}
          </label>
          <select
            id="status-filter"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value as ItemStatus | 'all')}
            className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
              focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
              focus-visible:outline-accent"
          >
            {STATUS_VALUES.map((value) => (
              <option key={value} value={value}>
                {statusOptionLabel(value, t)}
              </option>
            ))}
          </select>
          <form
            onSubmit={(event) => {
              event.preventDefault()
              setTagFilter(tagInput.trim())
            }}
          >
            <input
              type="search"
              aria-label={t('detail.tagFilter')}
              placeholder={t('detail.tagFilter')}
              value={tagInput}
              onChange={(event) => setTagInput(event.target.value)}
              onBlur={() => setTagFilter(tagInput.trim())}
              className="w-36 rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
                focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
                focus-visible:outline-accent"
            />
          </form>
          {canBulk && items.length > 0 && (
            <label className="flex items-center gap-2 text-sm text-ink">
              <input
                type="checkbox"
                aria-label={t('detail.selectAllLabel')}
                checked={allSelected}
                onChange={() =>
                  setSelected(allSelected ? new Set() : new Set(items.map((item) => item.id)))
                }
              />
              {t('detail.selectAll')}
            </label>
          )}
          <div className="ml-auto flex items-center gap-2">
            {createThumbnails.isSuccess && (
              <span className="text-xs text-muted" role="status">
                {t('detail.thumbnails.queued')}
              </span>
            )}
            {createThumbnails.isError && (
              <span className="text-xs text-danger" role="alert">
                {createThumbnails.error instanceof ApiError
                  ? (createThumbnails.error.detail ?? createThumbnails.error.title)
                  : t('detail.thumbnails.error')}
              </span>
            )}
            <Button
              variant="secondary"
              disabled={createThumbnails.isPending}
              // Scans queue this on their own for new items; the button covers
              // projects scanned before thumbnails existed (IMG-8).
              onClick={() => createThumbnails.mutate(undefined)}
            >
              {createThumbnails.isPending
                ? t('detail.thumbnails.queuing')
                : t('detail.thumbnails.generate')}
            </Button>
          </div>
        </div>

        {itemsQuery.isLoading && (
          <div className="flex justify-center py-16">
            <Spinner label={t('detail.items.loading')} />
          </div>
        )}

        {itemsQuery.isError && (
          <ErrorState
            title={t('detail.items.loadError')}
            message={
              itemsQuery.error instanceof ApiError
                ? (itemsQuery.error.detail ?? itemsQuery.error.title)
                : t('errors.unknown')
            }
            onRetry={() => void itemsQuery.refetch()}
          />
        )}

        {!itemsQuery.isLoading && !itemsQuery.isError && items.length === 0 && filtered && (
          <EmptyState title={t('detail.items.emptyTitle')} message={t('detail.items.emptyMessage')} />
        )}
        {!itemsQuery.isLoading && !itemsQuery.isError && items.length === 0 && !filtered && (
          // A new project: say how items get here instead of blaming a filter.
          <EmptyState
            title={t('detail.items.noneTitle')}
            message={t('detail.items.noneMessage')}
            action={
              <div className="flex flex-wrap justify-center gap-2">
                <Button onClick={() => setTab('data')}>{t('detail.items.addData')}</Button>
                <Link
                  to={`/projects/${projectId}/settings`}
                  className={`${LINK_BUTTON_CLASS} border border-line bg-surface text-ink hover:bg-line/40`}
                >
                  {t('nav.settings')}
                </Link>
              </div>
            }
          />
        )}

        {canBulk && selected.size > 0 && (
          <BulkActionsBar
            projectId={projectId}
            selectedIds={Array.from(selected)}
            labels={new Map(items.map((item) => [item.id, item.path]))}
            onClear={() => setSelected(new Set())}
            onApplied={() => {
              // Start the grid over from page one so the refetch does not
              // re-append pages that were already loaded.
              setSelected(new Set())
              setCursor(undefined)
            }}
          />
        )}
        {canBulk && selected.size > 0 && (
          <SplitControl
            projectId={projectId}
            items={items.filter((item) => selected.has(item.id))}
          />
        )}

        {items.length > 0 && (
          <>
            <ul className="grid grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5">
              {items.map((item) => (
                <li key={item.id} className="relative">
                  {canBulk && (
                    <input
                      type="checkbox"
                      aria-label={t('detail.items.selectItem', { path: item.path })}
                      checked={selected.has(item.id)}
                      onChange={() => toggleSelected(item.id)}
                      className="absolute left-2 top-2 z-10 h-4 w-4 accent-accent"
                    />
                  )}
                  <Link
                    to={`/projects/${projectId}/annotate/${item.id}`}
                    className="block overflow-hidden rounded-lg border border-line bg-surface
                      transition-colors hover:border-accent focus-visible:outline
                      focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
                  >
                    <div className="flex aspect-square items-center justify-center bg-line/20">
                      {item.media_type !== 'image' && !item.thumbnail_url ? (
                        // Only images get thumbnails; an <img> of a PDF or a
                        // video would just break.
                        <span
                          role="img"
                          aria-label={t(`detail.items.mediaKind.${item.media_type}`)}
                          className="text-4xl"
                          data-testid="media-kind-icon"
                        >
                          {MEDIA_ICONS[item.media_type]}
                        </span>
                      ) : item.thumbnail_url || item.media_url ? (
                        <img
                          // The generated thumbnail when the worker has made
                          // one, else the full object (IMG-8, UX-6).
                          src={item.thumbnail_url ?? item.media_url ?? undefined}
                          alt=""
                          loading="lazy"
                          decoding="async"
                          className="h-full w-full object-cover"
                          // A grid of broken-image icons says nothing; falling
                          // back to the same "No preview" text as an unsignable
                          // connector does.
                          onError={(event) => {
                            event.currentTarget.style.display = 'none'
                          }}
                        />
                      ) : (
                        <span className="text-xs text-muted">{t('detail.items.noPreview')}</span>
                      )}
                    </div>
                    <div className="flex items-center justify-between gap-2 p-2">
                      <span className="truncate text-xs text-ink" title={item.path}>
                        {item.path.split('/').pop()}
                      </span>
                      <StatusBadge status={item.status} />
                    </div>
                    {itemTags(item).length > 0 && (
                      <ul className="flex flex-wrap gap-1 px-2 pb-2" aria-label={t('detail.items.tagsLabel')}>
                        {itemTags(item).map((tag) => (
                          <li
                            key={tag}
                            className="rounded bg-line/40 px-1.5 py-0.5 text-[10px] text-muted"
                          >
                            {tag}
                          </li>
                        ))}
                      </ul>
                    )}
                  </Link>
                </li>
              ))}
            </ul>

            <div className="mt-6 flex justify-center">
              {itemsQuery.data?.next_cursor && (
                <Button
                  variant="secondary"
                  onClick={() => setCursor(itemsQuery.data?.next_cursor ?? undefined)}
                  disabled={itemsQuery.isFetching}
                >
                  {itemsQuery.isFetching ? t('common:loading') : t('detail.items.loadMore')}
                </Button>
              )}
            </div>
          </>
        )}
      </TabPanel>

      <TabPanel idPrefix="project" id="data" active={tab === 'data'}>
        <ScanPanel
          projectId={projectId}
          sourceConnectorId={project.data?.source_connector_id ?? null}
        />

        {projectId && <UploadPanel projectId={projectId} />}

        {projectId && <ImportPanel projectId={projectId} />}
      </TabPanel>

      <TabPanel idPrefix="project" id="exports" active={tab === 'exports'}>
        {projectId && (
          <section aria-labelledby="exports-heading" className="mb-8">
            <h2 id="exports-heading" className="mb-3 text-lg font-semibold text-ink">
              {t('detail.exports.heading')}
            </h2>

            <div className="mb-3 flex flex-wrap items-center gap-3">
              <label htmlFor="export-format" className="text-sm font-medium text-ink">
                {t('detail.exports.format')}
              </label>
              <select
                id="export-format"
                value={exportFormat}
                onChange={(event) => setExportFormat(event.target.value as ExportFormat)}
                className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
                  focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
                  focus-visible:outline-accent"
              >
                {EXPORT_FORMAT_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
              <Button
                variant="primary"
                disabled={createExport.isPending}
                onClick={() =>
                  createExport.mutate(
                    { format: exportFormat },
                    {
                      onSuccess: () => {
                        void queryClient.invalidateQueries({
                          queryKey: ['projects', projectId, 'jobs'],
                        })
                      },
                    },
                  )
                }
              >
                {createExport.isPending ? t('detail.exports.exporting') : t('detail.exports.export')}
              </Button>
            </div>

            {createExport.isError && (
              <p className="mb-3 text-sm text-danger">
                {createExport.error instanceof ApiError
                  ? (createExport.error.detail ?? createExport.error.title)
                  : t('detail.exports.exportFailed')}
              </p>
            )}

            {jobsQuery.isLoading && (
              <div className="flex justify-center py-6">
                <Spinner label={t('detail.exports.loading')} />
              </div>
            )}

            {jobsQuery.isError && (
              <ErrorState
                title={t('detail.exports.loadError')}
                message={
                  jobsQuery.error instanceof ApiError
                    ? (jobsQuery.error.detail ?? jobsQuery.error.title)
                    : t('errors.unknown')
                }
                onRetry={() => void jobsQuery.refetch()}
              />
            )}

            {jobsQuery.data && jobsQuery.data.items.length === 0 && (
              <p className="text-sm text-muted">{t('detail.exports.empty')}</p>
            )}

            {jobsQuery.data && jobsQuery.data.items.length > 0 && (
              <ul className="space-y-2">
                {jobsQuery.data.items.map((job) => (
                  <li
                    key={job.id}
                    className="flex items-center justify-between gap-3 rounded-md border border-line p-3 text-sm"
                  >
                    <div>
                      <p className="text-ink">
                        {String(job.payload.format)} · {job.status} · {job.progress}%
                      </p>
                      <p className="text-xs text-muted">{formatDateTime(job.created_at)}</p>
                      {job.status === 'failed' && job.error && (
                        <p className="text-xs text-danger">{job.error}</p>
                      )}
                    </div>
                    {job.status === 'succeeded' && (
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() =>
                          downloadExport.mutate(job.id, {
                            onSuccess: ({ url }) => {
                              window.open(url, '_blank', 'noopener')
                            },
                          })
                        }
                      >
                        {t('detail.exports.download')}
                      </Button>
                    )}
                    <JobRetryButton job={job} />
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}

        {projectId && <SnapshotsPanel projectId={projectId} />}
      </TabPanel>

      <TabPanel idPrefix="project" id="tasks" active={tab === 'tasks'}>
        {projectId && <TasksPanel projectId={projectId} items={items} />}

        {projectId && canBulk && <QualityPanel projectId={projectId} />}
      </TabPanel>

      <TabPanel idPrefix="project" id="prelabel" active={tab === 'prelabel'}>
        {projectId && (
          <section aria-labelledby="prelabel-heading" className="mb-8">
            <h2 id="prelabel-heading" className="mb-3 text-lg font-semibold text-ink">
              {t('detail.prelabel.heading')}
            </h2>

            {modelsQuery.data && models.length === 0 && (
              <p className="mb-3 text-sm text-muted">{t('detail.prelabel.noModels')}</p>
            )}

            <div className="mb-3 flex flex-wrap items-end gap-3">
              {models.length > 0 && (
                <div className="flex flex-col gap-1">
                  <label htmlFor="prelabel-model" className="text-sm font-medium text-ink">
                    {t('detail.prelabel.model')}
                  </label>
                  <select
                    id="prelabel-model"
                    value={prelabelModelId ?? ''}
                    onChange={(event) => setPrelabelModelId(event.target.value || undefined)}
                    className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
                      focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
                      focus-visible:outline-accent"
                  >
                    {models.map((model) => (
                      <option key={model.id} value={model.id}>
                        {model.name}
                      </option>
                    ))}
                  </select>
                </div>
              )}

              <div className="flex flex-col gap-1">
                <label htmlFor="prelabel-version" className="text-sm font-medium text-ink">
                  {t('detail.prelabel.version')}
                </label>
                <select
                  id="prelabel-version"
                  value={prelabelVersionId ?? ''}
                  onChange={(event) => setPrelabelVersionId(event.target.value || undefined)}
                  disabled={models.length === 0 || modelVersions.length === 0}
                  className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
                    disabled:opacity-50
                    focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
                    focus-visible:outline-accent"
                >
                  {modelVersions.map((version) => (
                    <option key={version.id} value={version.id}>
                      v{version.version}
                    </option>
                  ))}
                </select>
              </div>

              <div className="flex flex-col gap-1">
                <label htmlFor="prelabel-limit" className="text-sm font-medium text-ink">
                  {t('detail.prelabel.limit')}
                </label>
                <input
                  id="prelabel-limit"
                  type="number"
                  min={1}
                  value={prelabelLimit}
                  onChange={(event) => setPrelabelLimit(event.target.value)}
                  disabled={models.length === 0}
                  className="w-28 rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
                    disabled:opacity-50
                    focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
                    focus-visible:outline-accent"
                />
              </div>

              <div className="flex flex-col gap-1">
                <label htmlFor="prelabel-confidence" className="text-sm font-medium text-ink">
                  {t('detail.prelabel.minConfidence')}
                </label>
                <input
                  id="prelabel-confidence"
                  type="number"
                  min={0}
                  max={1}
                  step={0.05}
                  value={prelabelConfidence}
                  onChange={(event) => setPrelabelConfidence(Number(event.target.value))}
                  disabled={models.length === 0}
                  className="w-24 rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
                    disabled:opacity-50
                    focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
                    focus-visible:outline-accent"
                />
              </div>

              <label
                className="flex items-center gap-2 self-end pb-1.5 text-sm text-ink"
                title={t('detail.prelabel.queueUncertainTitle')}
              >
                <input
                  type="checkbox"
                  checked={prelabelPrioritize}
                  disabled={models.length === 0}
                  onChange={(event) => setPrelabelPrioritize(event.target.checked)}
                />
                {t('detail.prelabel.queueUncertain')}
              </label>

              <Button
                variant="primary"
                disabled={!prelabelVersionId || prelabelMutation.isPending}
                onClick={() =>
                  prelabelMutation.mutate({
                    model_version_id: prelabelVersionId as string,
                    limit: prelabelLimit === '' ? undefined : Number(prelabelLimit),
                    confidence_threshold: prelabelConfidence,
                    prioritize_uncertain: prelabelPrioritize || undefined,
                  })
                }
              >
                {prelabelMutation.isPending
                  ? t('detail.prelabel.submitting')
                  : t('detail.prelabel.submit')}
              </Button>
            </div>

            {prelabelMutation.isError && (
              <p className="mb-3 text-sm text-danger">
                {prelabelMutation.error instanceof ApiError
                  ? (prelabelMutation.error.detail ?? prelabelMutation.error.title)
                  : t('detail.prelabel.failed')}
              </p>
            )}

            {prelabelJobsQuery.isLoading && (
              <div className="flex justify-center py-6">
                <Spinner label={t('detail.prelabel.loading')} />
              </div>
            )}

            {prelabelJobsQuery.isError && (
              <ErrorState
                title={t('detail.prelabel.loadError')}
                message={
                  prelabelJobsQuery.error instanceof ApiError
                    ? (prelabelJobsQuery.error.detail ?? prelabelJobsQuery.error.title)
                    : t('errors.unknown')
                }
                onRetry={() => void prelabelJobsQuery.refetch()}
              />
            )}

            {prelabelJobsQuery.data && prelabelJobsQuery.data.items.length === 0 && (
              <p className="text-sm text-muted">{t('detail.prelabel.empty')}</p>
            )}

            {prelabelJobsQuery.data && prelabelJobsQuery.data.items.length > 0 && (
              <ul className="space-y-2">
                {prelabelJobsQuery.data.items.map((job) => (
                  <li key={job.id} className="rounded-md border border-line p-3 text-sm">
                    <p className="text-ink">
                      {job.status} · {job.progress}%
                    </p>
                    <p className="text-xs text-muted">{formatDateTime(job.created_at)}</p>
                    {job.status === 'succeeded' && (
                      <p className="text-xs text-muted">{prelabelSummary(job, t)}</p>
                    )}
                    {job.status === 'failed' && job.error && (
                      <p className="text-xs text-danger">{job.error}</p>
                    )}
                    <JobRetryButton job={job} />
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}
      </TabPanel>
    </div>
  )
}