import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '@/api/client'
import { useSeatReport } from '@/api/queries'
import type { SeatReport } from '@/api/types'
import { BusinessBadge, useBusinessFeature } from '@/components/BusinessBadge'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import i18n from '@/i18n'

const DATE_INPUT_CLASS =
  'rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink ' +
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent'

const CSV_HEADER = [
  'period_start',
  'period_end',
  'active_users',
  'peak_active_users',
  'peak_at',
  'overage',
]

function csvCell(value: string | number | null): string {
  const text = value === null ? '' : String(value)
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
}

/**
 * The seat report as CSV for the vendor (LIC-30): a `#` preamble naming the
 * install and licence, then one row per month.
 */
export function seatReportCsv(report: SeatReport): string {
  const preamble: Array<[string, string | number | null]> = [
    ['generated_at', report.generated_at],
    ['install_id', report.install_id],
    ['license_id', report.license_id],
    ['licensee', report.licensee],
    ['tier', report.tier],
    ['seats', report.seats],
    ['peak_active_users', report.peak_active_users],
    ['peak_overage', report.peak_overage],
  ]
  const lines = [
    ...preamble.map(([key, value]) => `# ${key}: ${value ?? ''}`),
    CSV_HEADER.join(','),
    ...report.periods.map((period) =>
      [
        period.start,
        period.end,
        period.active_users,
        period.peak_active_users,
        period.peak_at,
        period.overage,
      ]
        .map(csvCell)
        .join(','),
    ),
  ]
  return `${lines.join('\n')}\n`
}

function download(report: SeatReport): void {
  const blob = new Blob([seatReportCsv(report)], { type: 'text/csv' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `seat-report-${report.start}-${report.end}.csv`
  link.click()
  URL.revokeObjectURL(url)
}

function monthLabel(start: string): string {
  return new Date(`${start}T00:00:00Z`).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    timeZone: 'UTC',
  })
}

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? (error.detail ?? error.title) : i18n.t('admin:errors.unknown')
}

/** Distinct and peak active users per month, for offline true-up (LIC-30). */
export function SeatReportSection(): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const feature = useBusinessFeature('seat_report')
  const licensed = feature !== false
  const report = useSeatReport(start, end, feature !== false && feature !== undefined)
  const data = report.data

  if (!licensed) {
    return (
      <section aria-label={t('licence.seatReport.ariaLabel')} className="mb-8 rounded-lg border border-line p-4">
        <h2 className="mb-1 flex items-center gap-2 text-base font-semibold text-ink">
          {t('licence.seatReport.heading')} <BusinessBadge />
        </h2>
        <p className="max-w-prose text-sm text-muted">{t('licence.seatReport.locked')}</p>
      </section>
    )
  }

  return (
    <section aria-label={t('licence.seatReport.ariaLabel')} className="mb-8 rounded-lg border border-line p-4">
      <h2 className="mb-1 text-base font-semibold text-ink">{t('licence.seatReport.heading')}</h2>
      <p className="mb-3 max-w-prose text-sm text-muted">{t('licence.seatReport.description')}</p>
      <div className="mb-3 flex flex-wrap items-end gap-3 text-sm">
        <label className="flex flex-col gap-1 text-ink">
          {t('licence.seatReport.from')}
          <input
            type="date"
            className={DATE_INPUT_CLASS}
            value={start}
            onChange={(event) => setStart(event.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1 text-ink">
          {t('licence.seatReport.to')}
          <input
            type="date"
            className={DATE_INPUT_CLASS}
            value={end}
            onChange={(event) => setEnd(event.target.value)}
          />
        </label>
        <Button
          size="sm"
          variant="secondary"
          disabled={!data}
          onClick={() => data && download(data)}
        >
          {t('licence.seatReport.downloadCsv')}
        </Button>
      </div>

      {report.isLoading && <Spinner label={t('licence.seatReport.loading')} />}
      {report.isError && (
        <p role="alert" className="text-sm text-danger">
          {t('licence.seatReport.loadError', { message: errorMessage(report.error) })}
        </p>
      )}
      {data && (
        <>
          <p className="mb-2 text-sm text-ink">
            {t('licence.seatReport.peakBase', { peak: data.peak_active_users })}
            {data.seats !== null && t('licence.seatReport.peakForSeats', { seats: data.seats })}
            {data.peak_overage !== null &&
              data.peak_overage > 0 &&
              t('licence.seatReport.peakOverage', { overage: data.peak_overage })}
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-muted">
                <tr>
                  <th className="py-1 pr-4 font-normal">{t('licence.seatReport.table.month')}</th>
                  <th className="py-1 pr-4 font-normal">{t('licence.seatReport.table.activeUsers')}</th>
                  <th className="py-1 pr-4 font-normal">{t('licence.seatReport.table.peakAtOnce')}</th>
                  <th className="py-1 font-normal">{t('licence.seatReport.table.overage')}</th>
                </tr>
              </thead>
              <tbody className="text-ink">
                {data.periods.map((period) => (
                  <tr key={period.start} className="border-t border-line">
                    <td className="py-1 pr-4">{monthLabel(period.start)}</td>
                    <td className="py-1 pr-4">{period.active_users}</td>
                    <td className="py-1 pr-4">{period.peak_active_users}</td>
                    <td className="py-1">{period.overage ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  )
}
