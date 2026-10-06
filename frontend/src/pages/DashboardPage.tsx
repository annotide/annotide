import { useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import type { TFunction } from 'i18next'
import { useProject, useProjectStats } from '@/api/queries'
import type { ItemStatus, ProjectStats, TaskTypeStats, ThroughputDay } from '@/api/types'
import { Spinner } from '@/components/Spinner'
import { ErrorState } from '@/components/ErrorState'
import { EmptyState } from '@/components/EmptyState'

/**
 * Project dashboard (UX-5): progress, throughput, rejection rate, class balance.
 *
 * Every chart is plain HTML + Tailwind (no chart library): bars are `div`s
 * sized by percentage, and each carries its number as text so the page is
 * readable without colour and by screen readers.
 */

const WINDOW_OPTIONS = [7, 14, 30, 90] as const

const ITEM_STATUS_ORDER: ItemStatus[] = [
  'new',
  'prelabeled',
  'annotating',
  'submitted',
  'in_review',
  'approved',
  'rejected',
  'skipped',
]

const ITEM_STATUS_COLOR: Record<ItemStatus, string> = {
  new: 'bg-muted/40',
  prelabeled: 'bg-sky-400',
  annotating: 'bg-amber-400',
  submitted: 'bg-violet-400',
  in_review: 'bg-violet-600',
  approved: 'bg-emerald-500',
  rejected: 'bg-rose-500',
  skipped: 'bg-muted',
}

function formatPercent(fraction: number): string {
  return `${Math.round(fraction * 100)}%`
}

function statusLabel(status: ItemStatus, t: TFunction<'projects'>): string {
  return t(`status.${status}`)
}

export function DashboardPage(): JSX.Element {
  const { t } = useTranslation('projects')
  const { projectId } = useParams<{ projectId: string }>()
  const [days, setDays] = useState<number>(14)
  const project = useProject(projectId)
  const stats = useProjectStats(projectId, days)

  if (!projectId) {
    return <ErrorState title={t('errors.missingProjectId')} />
  }

  return (
    <div className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-6">
      <header className="mb-6 flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-sm text-muted">
            <Link to={`/projects/${projectId}`} className="hover:underline">
              {project.data?.name ?? t('dashboard.projectFallback')}
            </Link>
          </p>
          <h1 className="text-xl font-semibold text-ink">{t('dashboard.heading')}</h1>
        </div>
        <label className="flex items-center gap-2 text-sm text-muted">
          {t('dashboard.throughputWindow')}
          <select
            aria-label={t('dashboard.throughputWindow')}
            value={days}
            onChange={(event) => setDays(Number(event.target.value))}
            className="rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink"
          >
            {WINDOW_OPTIONS.map((option) => (
              <option key={option} value={option}>
                {t('dashboard.daysOption', { count: option })}
              </option>
            ))}
          </select>
        </label>
      </header>

      {stats.isLoading && (
        <div className="flex justify-center py-16">
          <Spinner label={t('dashboard.loading')} />
        </div>
      )}

      {stats.isError && (
        <ErrorState
          title={t('dashboard.loadError')}
          message={stats.error instanceof Error ? stats.error.message : undefined}
          onRetry={() => void stats.refetch()}
        />
      )}

      {stats.data && stats.data.items.total === 0 && (
        <EmptyState title={t('dashboard.empty.title')} message={t('dashboard.empty.message')} />
      )}

      {stats.data && stats.data.items.total > 0 && <Panels stats={stats.data} t={t} />}
    </div>
  )
}

function Panels({ stats, t }: { stats: ProjectStats; t: TFunction<'projects'> }): JSX.Element {
  const approved = stats.items.by_status.approved
  const progress = stats.items.total ? approved / stats.items.total : 0
  const openAnnotate = stats.tasks.annotate.open + stats.tasks.annotate.in_progress
  const openReview = stats.tasks.review.open + stats.tasks.review.in_progress

  return (
    <div className="space-y-8">
      <section
        aria-labelledby="dashboard-summary"
        className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4"
      >
        <h2 id="dashboard-summary" className="sr-only">
          {t('dashboard.summaryHeading')}
        </h2>
        <Tile
          label={t('dashboard.tiles.progress')}
          value={formatPercent(progress)}
          detail={t('dashboard.tiles.progressDetail', { approved, total: stats.items.total })}
        />
        <Tile
          label={t('dashboard.tiles.annotationQueue')}
          value={String(openAnnotate)}
          detail={t('dashboard.tiles.inProgress', { count: stats.tasks.annotate.in_progress })}
        />
        <Tile
          label={t('dashboard.tiles.reviewQueue')}
          value={String(openReview)}
          detail={t('dashboard.tiles.inProgress', { count: stats.tasks.review.in_progress })}
        />
        <Tile
          label={t('dashboard.tiles.rejectionRate')}
          value={formatPercent(stats.review.rejection_rate)}
          detail={t('dashboard.tiles.rejectionDetail', {
            rejected: stats.review.rejected,
            approved: stats.review.approved,
          })}
        />
      </section>

      <Card title={t('dashboard.cards.itemsByStatus')}>
        <StatusBar byStatus={stats.items.by_status} total={stats.items.total} t={t} />
        <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-4">
          {ITEM_STATUS_ORDER.map((status) => (
            <div key={status} className="flex items-center gap-2">
              <span
                aria-hidden="true"
                className={`h-2.5 w-2.5 rounded-sm ${ITEM_STATUS_COLOR[status]}`}
              />
              <dt className="capitalize text-muted">{statusLabel(status, t)}</dt>
              <dd className="ml-auto font-medium tabular-nums text-ink">
                {stats.items.by_status[status]}
              </dd>
            </div>
          ))}
        </dl>
      </Card>

      <Card
        title={t('dashboard.cards.throughputTitle')}
        subtitle={t('dashboard.cards.throughputSubtitle')}
      >
        <Throughput series={stats.throughput} t={t} />
      </Card>

      <div className="grid gap-8 lg:grid-cols-2 [&>*]:min-w-0">
        <Card
          title={t('dashboard.cards.classBalanceTitle')}
          subtitle={t('dashboard.cards.classBalanceSubtitle')}
        >
          {stats.classes.length === 0 ? (
            <p className="text-sm text-muted">{t('dashboard.noAnnotations')}</p>
          ) : (
            <ClassBalance classes={stats.classes} t={t} />
          )}
        </Card>

        <Card title={t('dashboard.cards.annotators')}>
          {stats.annotators.length === 0 ? (
            <p className="text-sm text-muted">{t('dashboard.noAnnotations')}</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-left text-xs uppercase tracking-wide text-muted">
                  <tr>
                    <th scope="col" className="py-1 font-medium">
                      {t('dashboard.table.annotator')}
                    </th>
                    <th scope="col" className="py-1 text-right font-medium">
                      {t('dashboard.table.submitted')}
                    </th>
                    <th scope="col" className="py-1 text-right font-medium">
                      {t('dashboard.table.approved')}
                    </th>
                    <th scope="col" className="py-1 text-right font-medium">
                      {t('dashboard.table.rejected')}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {stats.annotators.map((row) => (
                    <tr key={row.user_id} className="border-t border-line">
                      <td className="py-1.5 text-ink">{row.display_name}</td>
                      <td className="py-1.5 text-right tabular-nums text-ink">{row.submitted}</td>
                      <td className="py-1.5 text-right tabular-nums text-ink">{row.approved}</td>
                      <td className="py-1.5 text-right tabular-nums text-ink">{row.rejected}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>

      <div className="grid gap-8 lg:grid-cols-2 [&>*]:min-w-0">
        <Card title={t('dashboard.cards.tasks')}>
          <TaskTable annotate={stats.tasks.annotate} review={stats.tasks.review} t={t} />
        </Card>
        <Card title={t('dashboard.cards.annotationVersions')}>
          <dl className="grid grid-cols-2 gap-y-1 text-sm">
            <dt className="text-muted">{t('dashboard.versions.total')}</dt>
            <dd className="text-right tabular-nums text-ink">{stats.annotations.versions}</dd>
            <dt className="text-muted">{t('dashboard.versions.byHumans')}</dt>
            <dd className="text-right tabular-nums text-ink">
              {stats.annotations.by_source.human ?? 0}
            </dd>
            <dt className="text-muted">{t('dashboard.versions.byModels')}</dt>
            <dd className="text-right tabular-nums text-ink">
              {stats.annotations.by_source.model ?? 0}
            </dd>
            {(['draft', 'submitted', 'approved', 'rejected'] as const).map((status) => (
              <div key={status} className="contents">
                <dt className="capitalize text-muted">{t(`dashboard.versions.latest.${status}`)}</dt>
                <dd className="text-right tabular-nums text-ink">
                  {stats.annotations.latest_by_status[status]}
                </dd>
              </div>
            ))}
          </dl>
        </Card>
      </div>
    </div>
  )
}

function Tile({
  label,
  value,
  detail,
}: {
  label: string
  value: string
  detail: string
}): JSX.Element {
  return (
    <div className="rounded-lg border border-line bg-surface p-4">
      <p className="text-sm text-muted">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums text-ink">{value}</p>
      <p className="mt-1 text-xs text-muted">{detail}</p>
    </div>
  )
}

function Card({
  title,
  subtitle,
  children,
}: {
  title: string
  subtitle?: string
  children: React.ReactNode
}): JSX.Element {
  return (
    <section className="rounded-lg border border-line bg-surface p-4">
      <h2 className="text-base font-semibold text-ink">{title}</h2>
      {subtitle && <p className="mb-3 text-xs text-muted">{subtitle}</p>}
      {!subtitle && <div className="mb-3" />}
      {children}
    </section>
  )
}

function StatusBar({
  byStatus,
  total,
  t,
}: {
  byStatus: Record<ItemStatus, number>
  total: number
  t: TFunction<'projects'>
}): JSX.Element {
  return (
    <div
      role="img"
      aria-label={ITEM_STATUS_ORDER.map(
        (s) => `${statusLabel(s, t).toLowerCase()} ${byStatus[s]}`,
      ).join(', ')}
      className="flex h-4 w-full overflow-hidden rounded bg-line/40"
    >
      {ITEM_STATUS_ORDER.map((status) => {
        const count = byStatus[status]
        if (!count) return null
        return (
          <div
            key={status}
            className={ITEM_STATUS_COLOR[status]}
            style={{ width: `${(count / total) * 100}%` }}
            title={`${statusLabel(status, t)}: ${count}`}
          />
        )
      })}
    </div>
  )
}

function Throughput({ series, t }: { series: ThroughputDay[]; t: TFunction<'projects'> }): JSX.Element {
  const max = Math.max(1, ...series.map((d) => d.submitted + d.approved + d.rejected))
  const total = series.reduce((sum, d) => sum + d.submitted + d.approved + d.rejected, 0)
  return (
    <div>
      <ol
        aria-label={t('dashboard.throughput.ariaLabel')}
        className="flex h-40 items-end gap-1 border-b border-line"
        data-testid="throughput"
      >
        {series.map((day) => {
          const count = day.submitted + day.approved + day.rejected
          return (
            <li
              key={day.day}
              className="flex h-full flex-1 flex-col justify-end"
              title={t('dashboard.throughput.dayTitle', {
                day: day.day,
                submitted: day.submitted,
                approved: day.approved,
                rejected: day.rejected,
              })}
            >
              <span className="sr-only">
                {t('dashboard.throughput.dayVersions', { day: day.day, count })}
              </span>
              <div className="flex flex-col-reverse" style={{ height: `${(count / max) * 100}%` }}>
                <Segment count={day.submitted} total={count} className="bg-violet-400" />
                <Segment count={day.approved} total={count} className="bg-emerald-500" />
                <Segment count={day.rejected} total={count} className="bg-rose-500" />
              </div>
            </li>
          )
        })}
      </ol>
      <div className="mt-1 flex justify-between text-xs text-muted">
        <span>{series[0]?.day}</span>
        <span>
          {t('dashboard.throughput.totalDays', { total, days: series.length })}
        </span>
        <span>{series[series.length - 1]?.day}</span>
      </div>
      <Legend
        entries={[
          [t('dashboard.legend.submitted'), 'bg-violet-400'],
          [t('dashboard.legend.approved'), 'bg-emerald-500'],
          [t('dashboard.legend.rejected'), 'bg-rose-500'],
        ]}
      />
    </div>
  )
}

function Segment({ count, total, className }: { count: number; total: number; className: string }) {
  if (!count) return null
  return <div className={className} style={{ height: `${(count / total) * 100}%` }} />
}

function Legend({ entries }: { entries: [string, string][] }): JSX.Element {
  return (
    <ul className="mt-2 flex flex-wrap gap-4 text-xs text-muted">
      {entries.map(([label, color]) => (
        <li key={label} className="flex items-center gap-1.5">
          <span aria-hidden="true" className={`h-2.5 w-2.5 rounded-sm ${color}`} />
          {label}
        </li>
      ))}
    </ul>
  )
}

function ClassBalance({
  classes,
  t,
}: {
  classes: ProjectStats['classes']
  t: TFunction<'projects'>
}): JSX.Element {
  const max = Math.max(1, ...classes.map((c) => c.count))
  return (
    <ol className="space-y-1.5" aria-label={t('dashboard.classBalanceAriaLabel')}>
      {classes.map((row) => (
        <li
          key={row.label}
          className="grid grid-cols-[minmax(0,8rem)_1fr_auto] items-center gap-2 text-sm"
        >
          <span className="truncate text-ink" title={row.label}>
            {row.label}
          </span>
          <div className="h-3 rounded bg-line/40">
            <div
              className="h-3 rounded bg-accent"
              style={{ width: `${(row.count / max) * 100}%` }}
            />
          </div>
          <span className="tabular-nums text-muted">{row.count}</span>
        </li>
      ))}
    </ol>
  )
}

function TaskTable({
  annotate,
  review,
  t,
}: {
  annotate: TaskTypeStats
  review: TaskTypeStats
  t: TFunction<'projects'>
}): JSX.Element {
  const columns = ['open', 'in_progress', 'done', 'cancelled'] as const
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wide text-muted">
          <tr>
            <th scope="col" className="py-1 font-medium">
              {t('dashboard.table.type')}
            </th>
            {columns.map((column) => (
              <th key={column} scope="col" className="py-1 text-right font-medium">
                {t(`dashboard.taskColumns.${column}`)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {(
            [
              [t('dashboard.taskRows.annotate'), annotate],
              [t('dashboard.taskRows.review'), review],
            ] as const
          ).map(([label, row]) => (
            <tr key={label} className="border-t border-line">
              <td className="py-1.5 text-ink">{label}</td>
              {columns.map((column) => (
                <td key={column} className="py-1.5 text-right tabular-nums text-ink">
                  {row[column]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
