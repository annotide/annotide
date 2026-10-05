import { useTranslation } from 'react-i18next'
import { ApiError } from '@/api/client'
import { useRetryJob } from '@/api/queries'
import type { Job } from '@/api/types'
import { Button } from './Button'

/**
 * "Retry" for a failed or cancelled job (ARC-4): `POST /jobs/{id}/retry` runs
 * the same payload again. Renders nothing for any other status.
 */
export function JobRetryButton({ job }: { job: Job }) {
  const { t } = useTranslation('common')
  const retry = useRetryJob()

  if (job.status !== 'failed' && job.status !== 'cancelled') return null

  return (
    <span className="inline-flex items-center gap-2">
      <Button
        variant="secondary"
        size="sm"
        disabled={retry.isPending}
        onClick={() => retry.mutate(job.id)}
      >
        {retry.isPending ? t('job.retrying') : t('job.retry')}
      </Button>
      {retry.isError && (
        <span role="alert" className="text-xs text-danger">
          {retry.error instanceof ApiError
            ? (retry.error.detail ?? retry.error.title)
            : t('job.retryFailed')}
        </span>
      )}
    </span>
  )
}
