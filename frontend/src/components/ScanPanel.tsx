import { useEffect } from 'react'
import { Link } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useJob, useScanProject } from '@/api/queries'
import type { Job } from '@/api/types'
import { ApiError } from '@/api/client'
import { Button } from '@/components/Button'

/** How often the panel polls the scan it queued, until the scan settles. */
const SCAN_POLL_MS = 2000

function isInFlight(job: Job): boolean {
  return job.status === 'queued' || job.status === 'running'
}

interface ScanPanelProps {
  projectId: string
  /** The project's source connector; without one there is nothing to scan. */
  sourceConnectorId: string | null
}

/**
 * Registers files already in the source storage as items. Uploads queue a
 * scan of their own; this covers a bucket that was filled some other way.
 */
export function ScanPanel({ projectId, sourceConnectorId }: ScanPanelProps): JSX.Element {
  const { t } = useTranslation('projects')
  const scan = useScanProject()
  const queryClient = useQueryClient()
  const job = useJob(scan.data?.id, {
    refetchInterval: (query) =>
      query.state.data && !isInFlight(query.state.data) ? false : SCAN_POLL_MS,
  })
  const current = job.data ?? scan.data
  const status = current?.status
  useEffect(() => {
    if (status === 'succeeded') {
      void queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'items'] })
    }
  }, [status, projectId, queryClient])

  const created = current?.result?.items_created
  const busy = scan.isPending || (current !== undefined && isInFlight(current))

  return (
    <section aria-labelledby="scan-heading" className="mb-8">
      <h2 id="scan-heading" className="mb-3 text-lg font-semibold text-ink">
        {t('scan.heading')}
      </h2>
      {sourceConnectorId ? (
        <div className="flex flex-wrap items-center gap-3">
          <p className="text-sm text-muted">{t('scan.hint')}</p>
          <Button variant="secondary" disabled={busy} onClick={() => scan.mutate(projectId)}>
            {busy ? t('scan.scanning') : t('scan.button')}
          </Button>
        </div>
      ) : (
        <p className="text-sm text-muted">
          {t('scan.noSource')}{' '}
          <Link to={`/projects/${projectId}/settings`} className="text-accent underline">
            {t('scan.openSettings')}
          </Link>
        </p>
      )}
      {scan.isError && (
        <p role="alert" className="mt-2 text-sm text-danger">
          {scan.error instanceof ApiError
            ? (scan.error.detail ?? scan.error.title)
            : t('scan.failed')}
        </p>
      )}
      {status && (
        <p role="status" className="mt-2 text-sm text-ink">
          {status === 'succeeded'
            ? t('scan.done', { count: typeof created === 'number' ? created : 0 })
            : status === 'failed' || status === 'cancelled'
              ? (current?.error ?? t('scan.failed'))
              : t('scan.queued')}
        </p>
      )}
    </section>
  )
}
