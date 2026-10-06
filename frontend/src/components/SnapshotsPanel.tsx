import { Fragment, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQueryClient } from '@tanstack/react-query'
import {
  useCreateExport,
  useCreateSnapshot,
  useJobs,
  useMlPlatforms,
  usePublishSnapshot,
  useRequestRetrain,
  useSnapshotDiff,
  useSnapshotLineage,
  useSnapshots,
} from '@/api/queries'
import { ApiError } from '@/api/client'
import type {
  ItemStatus,
  Job,
  RetrainResult,
  SnapshotPublishResult,
  Snapshot,
  SnapshotCreate,
  SplitName,
} from '@/api/types'
import { Button } from '@/components/Button'
import { JobRetryButton } from '@/components/JobRetryButton'
import { Spinner } from '@/components/Spinner'
import { ErrorState } from '@/components/ErrorState'
import { safeHref } from '../lib/url'

const ITEM_STATUSES: ItemStatus[] = [
  'new',
  'prelabeled',
  'annotating',
  'submitted',
  'in_review',
  'approved',
  'rejected',
  'skipped',
]
const EXPORT_FORMATS = ['coco', 'yolo', 'yolo_pose', 'native', 'spacy', 'conll'] as const
const SPLIT_NAMES: SplitName[] = ['train', 'val', 'test']

interface LineageDetailsProps {
  projectId: string
  snapshot: Snapshot
}

/** Model versions trained on a snapshot (EXP-8), fetched when the row is expanded. */
function LineageDetails({ projectId, snapshot }: LineageDetailsProps): JSX.Element {
  const { t } = useTranslation('projects')
  const lineageQuery = useSnapshotLineage(projectId, snapshot.id)
  if (lineageQuery.isLoading) return <Spinner label={t('snapshots.lineage.loading')} />
  if (lineageQuery.isError) {
    return (
      <ErrorState
        title={t('snapshots.lineage.loadError')}
        message={errorText(lineageQuery.error, t('snapshots.lineage.tryAgain'))}
      />
    )
  }
  const versions = lineageQuery.data?.versions ?? []
  if (versions.length === 0) {
    return (
      <p className="text-xs text-muted" data-testid={`lineage-${snapshot.id}`}>
        {t('snapshots.lineage.none', { name: snapshot.name })}
      </p>
    )
  }
  return (
    <ul className="space-y-1 text-xs text-ink" data-testid={`lineage-${snapshot.id}`}>
      {versions.map((version) => {
        const run = version.training_run
        const runId =
          run && (typeof run.id === 'string' || typeof run.id === 'number') ? String(run.id) : null
        const runUrl = safeHref(run?.url)
        return (
          <li key={version.id} className="flex flex-wrap items-center gap-x-2">
            <span className="font-medium">
              {version.model_name} v{version.version}
            </span>
            <span className="text-muted">{new Date(version.created_at).toLocaleString()}</span>
            {runId && (
              <span className="text-muted">
                {t('snapshots.lineage.run')}{' '}
                {runUrl ? (
                  <a href={runUrl} target="_blank" rel="noreferrer" className="underline">
                    {runId}
                  </a>
                ) : (
                  runId
                )}
              </span>
            )}
            <span className="text-muted">
              {version.items_predicted === 0
                ? t('snapshots.lineage.noItemsPredicted')
                : t('snapshots.lineage.itemsPredicted', { count: version.items_predicted })}
            </span>
            {version.snapshot_digest && version.snapshot_digest !== snapshot.digest && (
              <span className="text-danger">{t('snapshots.lineage.digestDiffers')}</span>
            )}
          </li>
        )
      })}
    </ul>
  )
}

export interface SnapshotsPanelProps {
  projectId: string
}

function isInFlight(job: Job): boolean {
  return job.status === 'queued' || job.status === 'running'
}

function errorText(error: unknown, fallback: string): string {
  return error instanceof ApiError ? (error.detail ?? error.title) : fallback
}

/** "80 / 10 / 10 by folder" for a snapshot row. */
export function describeSplit(snapshot: Snapshot): string | null {
  const split = snapshot.split
  if (!split) return null
  const pct = (n: number) => Math.round(n * 100)
  const base = `${pct(split.train)} / ${pct(split.val)} / ${pct(split.test)}`
  return split.group_by ? `${base} by ${split.group_by}` : base
}

/** Frozen datasets (EXP-1): create one from a filter with an optional
 * train / val / test split (EXP-3), export a snapshot or one of its splits
 * (EXP-5), and compare two snapshots (EXP-4). */
export function SnapshotsPanel({ projectId }: SnapshotsPanelProps): JSX.Element {
  const { t } = useTranslation('projects')
  const queryClient = useQueryClient()
  const snapshotsQuery = useSnapshots(projectId, { limit: 20 })
  const jobsQuery = useJobs(
    projectId,
    { type: 'snapshot', limit: 5 },
    {
      refetchInterval: (query) => {
        const jobs = query.state.data?.items ?? []
        return jobs.some(isInFlight) ? 3000 : false
      },
    },
  )
  const createSnapshot = useCreateSnapshot(projectId)
  const createExport = useCreateExport(projectId)
  const requestRetrain = useRequestRetrain(projectId)
  const mlPlatformsQuery = useMlPlatforms()
  const publishSnapshot = usePublishSnapshot(projectId)
  const mlPlatforms = mlPlatformsQuery.data?.items ?? []
  const [mlPlatformId, setMlPlatformId] = useState('')
  const mlPlatform = mlPlatforms.find((p) => p.id === mlPlatformId) ?? null
  const [published, setPublished] = useState<{
    snapshot: Snapshot
    result: SnapshotPublishResult
  } | null>(null)

  // Create form
  const [name, setName] = useState('')
  const [statuses, setStatuses] = useState<ItemStatus[]>([])
  const [pathPrefix, setPathPrefix] = useState('')
  const [withSplit, setWithSplit] = useState(false)
  const [train, setTrain] = useState('80')
  const [val, setVal] = useState('10')
  const [test, setTest] = useState('10')
  const [seed, setSeed] = useState('0')
  const [groupBy, setGroupBy] = useState<'none' | 'folder' | 'meta'>('none')
  const [metaKey, setMetaKey] = useState('')
  const [formError, setFormError] = useState<string | null>(null)

  // Row actions
  const [exportFormat, setExportFormat] = useState<(typeof EXPORT_FORMATS)[number]>('coco')
  const [exportSplit, setExportSplit] = useState<Record<string, SplitName | 'all'>>({})
  const [exported, setExported] = useState<string | null>(null)
  const [retrained, setRetrained] = useState<{ snapshot: Snapshot; result: RetrainResult } | null>(
    null,
  )
  const [lineageId, setLineageId] = useState<string | null>(null)
  const [baseId, setBaseId] = useState<string | undefined>(undefined)
  const [targetId, setTargetId] = useState<string | undefined>(undefined)
  const diffQuery = useSnapshotDiff(projectId, baseId, targetId)

  // A snapshot job that just finished means a new row: refresh the list once.
  const inFlight = (jobsQuery.data?.items ?? []).filter(isInFlight).length
  useEffect(() => {
    void queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'snapshots'] })
  }, [inFlight, projectId, queryClient])

  const snapshots = snapshotsQuery.data?.items ?? []

  function buildBody(): SnapshotCreate | null {
    if (!name.trim()) {
      setFormError(t('snapshots.form.errors.nameRequired'))
      return null
    }
    const filter: Record<string, unknown> = {}
    if (statuses.length > 0) filter.item_status = statuses
    if (pathPrefix.trim()) filter.path_prefix = pathPrefix.trim()
    const body: SnapshotCreate = { name: name.trim(), filter }
    if (withSplit) {
      const ratios = [train, val, test].map((v) => Number.parseFloat(v))
      if (ratios.some((r) => Number.isNaN(r) || r < 0)) {
        setFormError(t('snapshots.form.errors.ratiosNotNumbers'))
        return null
      }
      const total = ratios.reduce((a, b) => a + b, 0)
      if (total <= 0) {
        setFormError(t('snapshots.form.errors.ratiosZero'))
        return null
      }
      const seedValue = Number.parseInt(seed, 10)
      if (Number.isNaN(seedValue)) {
        setFormError(t('snapshots.form.errors.seedInteger'))
        return null
      }
      if (groupBy === 'meta' && !metaKey.trim()) {
        setFormError(t('snapshots.form.errors.metaKeyRequired'))
        return null
      }
      // Percentages or fractions both work: normalise to a sum of 1.
      body.split = {
        train: ratios[0] / total,
        val: ratios[1] / total,
        test: ratios[2] / total,
        seed: seedValue,
        group_by:
          groupBy === 'none' ? null : groupBy === 'folder' ? 'folder' : `meta.${metaKey.trim()}`,
      }
    }
    return body
  }

  function handleCreate(): void {
    const body = buildBody()
    if (!body) return
    setFormError(null)
    createSnapshot.mutate(body, {
      onSuccess: () => setName(''),
      onError: (err) => setFormError(errorText(err, t('snapshots.form.errors.createFailed'))),
    })
  }

  function handleExport(snapshot: Snapshot): void {
    const split = exportSplit[snapshot.id] ?? 'all'
    const payload: Record<string, unknown> = { format: exportFormat, snapshot_id: snapshot.id }
    if (split !== 'all') payload.split = split
    setExported(null)
    createExport.mutate(payload, {
      onSuccess: () => {
        setExported(snapshot.id)
        void queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'jobs'] })
      },
    })
  }

  function handleRetrain(snapshot: Snapshot): void {
    setRetrained(null)
    // A Databricks platform with a job runs it too (API-6); others only get the webhook.
    const runsJob = mlPlatform?.kind === 'databricks' && mlPlatform.config.job_id !== undefined
    requestRetrain.mutate(
      { snapshot_id: snapshot.id, ml_platform_id: runsJob ? mlPlatform.id : undefined },
      { onSuccess: (result) => setRetrained({ snapshot, result }) },
    )
  }

  function handlePublish(snapshot: Snapshot): void {
    if (!mlPlatform) return
    setPublished(null)
    publishSnapshot.mutate(
      { snapshotId: snapshot.id, payload: { ml_platform_id: mlPlatform.id } },
      { onSuccess: (result) => setPublished({ snapshot, result }) },
    )
  }

  const inputClass =
    'rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent'
  const diff = diffQuery.data

  return (
    <section aria-labelledby="snapshots-heading" className="mb-8">
      <h2 id="snapshots-heading" className="mb-1 text-lg font-semibold text-ink">
        {t('snapshots.heading')}
      </h2>
      <p className="mb-3 text-sm text-muted">{t('snapshots.intro')}</p>

      <form
        aria-label={t('snapshots.form.ariaLabel')}
        className="mb-4 rounded-lg border border-line p-3"
        onSubmit={(e) => {
          e.preventDefault()
          handleCreate()
        }}
      >
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs text-muted">
            {t('snapshots.form.name')}
            <input
              type="text"
              aria-label={t('snapshots.form.nameAria')}
              value={name}
              onChange={(e) => setName(e.target.value)}
              className={inputClass}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-muted">
            {t('snapshots.form.itemStatus')}
            <select
              multiple
              aria-label={t('snapshots.form.itemStatusAria')}
              value={statuses}
              onChange={(e) =>
                setStatuses(Array.from(e.target.selectedOptions).map((o) => o.value as ItemStatus))
              }
              className={`${inputClass} h-24`}
            >
              {ITEM_STATUSES.map((status) => (
                <option key={status} value={status}>
                  {t(`status.${status}`)}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-xs text-muted">
            {t('snapshots.form.pathPrefix')}
            <input
              type="text"
              aria-label={t('snapshots.form.pathPrefixAria')}
              value={pathPrefix}
              placeholder="images/"
              onChange={(e) => setPathPrefix(e.target.value)}
              className={inputClass}
            />
          </label>
          <label className="flex items-center gap-2 text-sm text-ink">
            <input
              type="checkbox"
              aria-label={t('snapshots.form.splitCheckboxAria')}
              checked={withSplit}
              onChange={(e) => setWithSplit(e.target.checked)}
            />
            {t('snapshots.form.splitLabel')}
          </label>
        </div>

        {withSplit && (
          <div className="mt-3 flex flex-wrap items-end gap-3">
            {(
              [
                [t('snapshots.form.trainLabel'), train, setTrain],
                [t('snapshots.form.valLabel'), val, setVal],
                [t('snapshots.form.testLabel'), test, setTest],
              ] as const
            ).map(([label, value, setter]) => (
              <label key={label} className="flex flex-col gap-1 text-xs text-muted">
                {label} %
                <input
                  type="number"
                  aria-label={t('snapshots.form.ratioAria', { label })}
                  value={value}
                  min={0}
                  onChange={(e) => setter(e.target.value)}
                  className={`${inputClass} w-20`}
                />
              </label>
            ))}
            <label className="flex flex-col gap-1 text-xs text-muted">
              {t('snapshots.form.seedLabel')}
              <input
                type="number"
                aria-label={t('snapshots.form.seedAria')}
                value={seed}
                onChange={(e) => setSeed(e.target.value)}
                className={`${inputClass} w-20`}
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted">
              {t('snapshots.form.groupByLabel')}
              <select
                aria-label={t('snapshots.form.groupByAria')}
                value={groupBy}
                onChange={(e) => setGroupBy(e.target.value as 'none' | 'folder' | 'meta')}
                className={inputClass}
              >
                <option value="none">{t('snapshots.form.groupByNone')}</option>
                <option value="folder">{t('snapshots.form.groupByFolder')}</option>
                <option value="meta">{t('snapshots.form.groupByMeta')}</option>
              </select>
            </label>
            {groupBy === 'meta' && (
              <label className="flex flex-col gap-1 text-xs text-muted">
                {t('snapshots.form.metaKeyLabel')}
                <input
                  type="text"
                  aria-label={t('snapshots.form.metaKeyAria')}
                  value={metaKey}
                  placeholder="patient_id"
                  onChange={(e) => setMetaKey(e.target.value)}
                  className={inputClass}
                />
              </label>
            )}
          </div>
        )}

        <div className="mt-3 flex items-center gap-3">
          <Button type="submit" disabled={createSnapshot.isPending}>
            {createSnapshot.isPending ? t('snapshots.form.queuing') : t('snapshots.form.create')}
          </Button>
          {formError && (
            <span className="text-sm text-danger" role="alert">
              {formError}
            </span>
          )}
        </div>
      </form>

      {(jobsQuery.data?.items ?? []).some((job) => isInFlight(job) || job.status === 'failed') && (
        <ul className="mb-3 space-y-1 text-sm" aria-label={t('snapshots.jobs.ariaLabel')}>
          {(jobsQuery.data?.items ?? [])
            .filter((job) => isInFlight(job) || job.status === 'failed')
            .map((job) => (
              <li key={job.id} className="text-muted">
                {String(job.payload.name ?? t('snapshots.jobs.defaultName'))}:{' '}
                {job.status.replace('_', ' ')}
                {job.status === 'failed' && job.error && (
                  <span className="text-danger"> — {job.error}</span>
                )}{' '}
                <JobRetryButton job={job} />
              </li>
            ))}
        </ul>
      )}

      {snapshotsQuery.isLoading && (
        <div className="flex justify-center py-6">
          <Spinner label={t('snapshots.loading')} />
        </div>
      )}
      {snapshotsQuery.isError && (
        <ErrorState
          title={t('snapshots.loadError')}
          message={errorText(snapshotsQuery.error, t('snapshots.unknownError'))}
          onRetry={() => void snapshotsQuery.refetch()}
        />
      )}
      {snapshotsQuery.data && snapshots.length === 0 && (
        <p className="text-sm text-muted">{t('snapshots.empty')}</p>
      )}

      {snapshots.length > 0 && (
        <>
          <div className="mb-2 flex flex-wrap items-center gap-3 text-sm">
            <label htmlFor="snapshot-export-format" className="font-medium text-ink">
              {t('snapshots.exportFormat')}
            </label>
            <select
              id="snapshot-export-format"
              value={exportFormat}
              onChange={(e) => setExportFormat(e.target.value as (typeof EXPORT_FORMATS)[number])}
              className={inputClass}
            >
              {EXPORT_FORMATS.map((f) => (
                <option key={f} value={f}>
                  {f.toUpperCase()}
                </option>
              ))}
            </select>
            {createExport.isError && (
              <span className="text-danger" role="alert">
                {errorText(createExport.error, t('snapshots.exportFailed'))}
              </span>
            )}
            {exported && !createExport.isError && (
              <span className="text-muted" role="status">
                {t('snapshots.exportQueued')}
              </span>
            )}
            {mlPlatforms.length > 0 && (
              <>
                <label htmlFor="snapshot-ml-platform" className="font-medium text-ink">
                  {t('snapshots.mlPlatform')}
                </label>
                <select
                  id="snapshot-ml-platform"
                  value={mlPlatformId}
                  onChange={(e) => setMlPlatformId(e.target.value)}
                  className={inputClass}
                >
                  <option value="">{t('snapshots.mlPlatformNone')}</option>
                  {mlPlatforms.map((platform) => (
                    <option key={platform.id} value={platform.id}>
                      {platform.name}
                    </option>
                  ))}
                </select>
              </>
            )}
            {publishSnapshot.isError && (
              <span className="text-danger" role="alert">
                {errorText(publishSnapshot.error, t('snapshots.publishFailed'))}
              </span>
            )}
            {published && !publishSnapshot.isError && (
              <span className="text-muted" role="status">
                {published.result.created
                  ? t('snapshots.published', {
                      name: published.snapshot.name,
                      experiment: published.result.experiment_name,
                    })
                  : t('snapshots.alreadyPublished', {
                      name: published.snapshot.name,
                      experiment: published.result.experiment_name,
                    })}{' '}
                {safeHref(published.result.run_url) && (
                  <a
                    href={safeHref(published.result.run_url)}
                    target="_blank"
                    rel="noreferrer"
                    className="underline"
                  >
                    {t('snapshots.openRun')}
                  </a>
                )}
              </span>
            )}
            {requestRetrain.isError && (
              <span className="text-danger" role="alert">
                {errorText(requestRetrain.error, t('snapshots.retrainFailed'))}
              </span>
            )}
            {retrained && !requestRetrain.isError && (
              <span className="text-muted" role="status">
                {retrained.result.ml_run
                  ? t('snapshots.retrainJobStarted', {
                      name: retrained.snapshot.name,
                      run: retrained.result.ml_run.run_id,
                    })
                  : retrained.result.deliveries === 0
                    ? t('snapshots.retrainNoWebhook', { name: retrained.snapshot.name })
                    : t('snapshots.retrainRequested', {
                        name: retrained.snapshot.name,
                        count: retrained.result.deliveries,
                      })}
                {safeHref(retrained.result.ml_run?.run_url) && (
                  <>
                    {' '}
                    <a
                      href={safeHref(retrained.result.ml_run?.run_url)}
                      target="_blank"
                      rel="noreferrer"
                      className="underline"
                    >
                      {t('snapshots.openRun')}
                    </a>
                  </>
                )}
              </span>
            )}
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-line text-muted">
                  <th className="py-1.5 pr-2 font-medium">{t('snapshots.table.name')}</th>
                  <th className="py-1.5 pr-2 font-medium">{t('snapshots.table.created')}</th>
                  <th className="py-1.5 pr-2 font-medium">{t('snapshots.table.items')}</th>
                  <th className="py-1.5 pr-2 font-medium">{t('snapshots.table.split')}</th>
                  <th className="py-1.5 pr-2 font-medium">{t('snapshots.table.digest')}</th>
                  <th className="py-1.5 pr-2 font-medium">{t('snapshots.table.export')}</th>
                  <th className="py-1.5 pr-2 font-medium">{t('snapshots.table.compare')}</th>
                  <th className="py-1.5 pr-2 font-medium">{t('snapshots.table.lineage')}</th>
                </tr>
              </thead>
              <tbody>
                {snapshots.map((snapshot) => (
                  <Fragment key={snapshot.id}>
                    <tr
                      className="border-b border-line/50"
                      data-testid={`snapshot-row-${snapshot.id}`}
                    >
                      <td className="py-1.5 pr-2 text-ink">{snapshot.name}</td>
                      <td className="py-1.5 pr-2 text-ink">
                        {new Date(snapshot.created_at).toLocaleString()}
                      </td>
                      <td className="py-1.5 pr-2 text-ink">{snapshot.item_count}</td>
                      <td className="py-1.5 pr-2 text-ink">{describeSplit(snapshot) ?? '—'}</td>
                      <td
                        className="py-1.5 pr-2 font-mono text-xs text-muted"
                        title={snapshot.digest}
                      >
                        {snapshot.digest.slice(0, 12)}
                      </td>
                      <td className="py-1.5 pr-2">
                        <div className="flex items-center gap-2">
                          {snapshot.split && (
                            <select
                              aria-label={t('snapshots.row.splitToExportAria', {
                                name: snapshot.name,
                              })}
                              value={exportSplit[snapshot.id] ?? 'all'}
                              onChange={(e) =>
                                setExportSplit((prev) => ({
                                  ...prev,
                                  [snapshot.id]: e.target.value as SplitName | 'all',
                                }))
                              }
                              className={inputClass}
                            >
                              <option value="all">{t('snapshots.row.allSplits')}</option>
                              {SPLIT_NAMES.map((s) => (
                                <option key={s} value={s}>
                                  {s}
                                </option>
                              ))}
                            </select>
                          )}
                          <Button
                            variant="secondary"
                            size="sm"
                            disabled={createExport.isPending}
                            onClick={() => handleExport(snapshot)}
                          >
                            {t('snapshots.row.export')}
                          </Button>
                          <Button
                            variant="secondary"
                            size="sm"
                            disabled={requestRetrain.isPending}
                            onClick={() => handleRetrain(snapshot)}
                            title={t('snapshots.row.retrainTitle')}
                          >
                            {t('snapshots.row.retrain')}
                          </Button>
                          {mlPlatform && (
                            <Button
                              variant="secondary"
                              size="sm"
                              disabled={publishSnapshot.isPending}
                              onClick={() => handlePublish(snapshot)}
                              aria-label={t('snapshots.row.publishAria', {
                                name: snapshot.name,
                                platform: mlPlatform.name,
                              })}
                            >
                              {t('snapshots.row.publish')}
                            </Button>
                          )}
                        </div>
                      </td>
                      <td className="py-1.5 pr-2">
                        <div className="flex items-center gap-2 text-xs text-ink">
                          <label className="flex items-center gap-1">
                            <input
                              type="radio"
                              name="diff-base"
                              aria-label={t('snapshots.row.compareFromAria', {
                                name: snapshot.name,
                              })}
                              checked={baseId === snapshot.id}
                              onChange={() => setBaseId(snapshot.id)}
                            />
                            {t('snapshots.row.from')}
                          </label>
                          <label className="flex items-center gap-1">
                            <input
                              type="radio"
                              name="diff-target"
                              aria-label={t('snapshots.row.compareToAria', {
                                name: snapshot.name,
                              })}
                              checked={targetId === snapshot.id}
                              onChange={() => setTargetId(snapshot.id)}
                            />
                            {t('snapshots.row.to')}
                          </label>
                        </div>
                      </td>
                      <td className="py-1.5 pr-2">
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-expanded={lineageId === snapshot.id}
                          aria-label={t('snapshots.row.lineageAria', { name: snapshot.name })}
                          onClick={() =>
                            setLineageId((prev) => (prev === snapshot.id ? null : snapshot.id))
                          }
                        >
                          {lineageId === snapshot.id
                            ? t('snapshots.row.hide')
                            : t('snapshots.row.show')}
                        </Button>
                      </td>
                    </tr>
                    {lineageId === snapshot.id && (
                      <tr className="border-b border-line/50">
                        <td colSpan={8} className="py-2 pr-2">
                          <LineageDetails projectId={projectId} snapshot={snapshot} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      {baseId && targetId && baseId === targetId && (
        <p className="mt-3 text-sm text-muted">{t('snapshots.compare.pickDifferent')}</p>
      )}
      {diffQuery.isLoading && (
        <div className="mt-3 flex justify-center py-4">
          <Spinner label={t('snapshots.compare.comparing')} />
        </div>
      )}
      {diffQuery.isError && (
        <p className="mt-3 text-sm text-danger" role="alert">
          {errorText(diffQuery.error, t('snapshots.compare.compareError'))}
        </p>
      )}
      {diff && (
        <div
          className="mt-3 rounded-lg border border-line p-3 text-sm"
          role="region"
          aria-label={t('snapshots.compare.regionAria')}
        >
          <p className="mb-2 text-ink">
            <span className="font-medium">{diff.base.name}</span> →{' '}
            <span className="font-medium">{diff.target.name}</span>:{' '}
            {t('snapshots.compare.summary', {
              added: diff.items.added,
              removed: diff.items.removed,
              changed: diff.items.changed,
              unchanged: diff.items.unchanged,
            })}
            {diff.items.split_moved > 0 &&
              t('snapshots.compare.splitMoved', { count: diff.items.split_moved })}
            {diff.truncated && t('snapshots.compare.truncated')}
          </p>
          {diff.classes.length > 0 && (
            <table className="mb-2 text-xs">
              <thead>
                <tr className="text-muted">
                  <th className="pr-3 text-left font-medium">
                    {t('snapshots.compare.classHeading')}
                  </th>
                  <th className="pr-3 text-right font-medium">{diff.base.name}</th>
                  <th className="pr-3 text-right font-medium">{diff.target.name}</th>
                  <th className="text-right font-medium">{t('snapshots.compare.delta')}</th>
                </tr>
              </thead>
              <tbody>
                {diff.classes.slice(0, 12).map((row) => (
                  <tr key={row.name}>
                    <td className="pr-3 text-ink">{row.name}</td>
                    <td className="pr-3 text-right text-ink">{row.base}</td>
                    <td className="pr-3 text-right text-ink">{row.target}</td>
                    <td
                      className={`text-right ${
                        row.delta > 0
                          ? 'text-success'
                          : row.delta < 0
                            ? 'text-danger'
                            : 'text-muted'
                      }`}
                    >
                      {row.delta > 0 ? `+${row.delta}` : row.delta}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {diff.changed.length > 0 && (
            <details className="text-xs text-muted">
              <summary className="cursor-pointer">
                {t('snapshots.compare.changedItems', { count: diff.items.changed })}
              </summary>
              <ul className="mt-1 list-disc pl-5">
                {diff.changed.map((row) => (
                  <li key={row.item_id}>
                    <span className="font-mono text-ink">{row.path}</span> v{row.from_version} → v
                    {row.to_version}: +{row.shapes.added} / −{row.shapes.removed} / ~
                    {row.shapes.changed} shapes
                  </li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
    </section>
  )
}
