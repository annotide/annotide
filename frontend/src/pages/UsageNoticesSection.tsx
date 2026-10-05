import { useTranslation } from 'react-i18next'

import { ApiError } from '@/api/client'
import { useUsageNotices } from '@/api/queries'
import i18n from '@/i18n'

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? (error.detail ?? error.title) : i18n.t('admin:errors.unknown')
}

/**
 * Signs that one seat is used by several people (LIC-31). Computed on this
 * installation, never sent anywhere and never enforced.
 */
export function UsageNoticesSection(): JSX.Element {
  const { t } = useTranslation('admin')
  const notices = useUsageNotices()
  const data = notices.data

  return (
    <section aria-label={t('licence.usageNotices.ariaLabel')} className="mb-8 rounded-lg border border-line p-4">
      <h2 className="mb-1 text-base font-semibold text-ink">{t('licence.usageNotices.heading')}</h2>
      <p className="mb-3 max-w-prose text-sm text-muted">
        {t('licence.usageNotices.description', { days: data?.window_days ?? 30 })}
      </p>
      {notices.isError && (
        <p role="alert" className="text-sm text-danger">
          {t('licence.usageNotices.loadError', { message: errorMessage(notices.error) })}
        </p>
      )}
      {data && data.notices.length === 0 && (
        <p className="text-sm text-ink">{t('licence.usageNotices.empty')}</p>
      )}
      {data && data.notices.length > 0 && (
        <ul className="flex flex-col gap-2">
          {data.notices.map((notice) => (
            <li
              key={`${notice.kind}-${notice.user_id}`}
              className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2 text-sm"
            >
              <p className="font-medium text-ink">
                {t(`licence.usageNotices.kinds.${notice.kind}`)} — {notice.display_name}{' '}
                <span className="font-normal text-muted">({notice.email})</span>
              </p>
              <p className="text-ink">{notice.detail}</p>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
