import { useState } from 'react'
import type { FormEvent } from 'react'
import { Trans, useTranslation } from 'react-i18next'
import type { TFunction } from 'i18next'

import { ApiError } from '@/api/client'
import { useInstallLicense, useLicense, useRemoveLicense, useStartTrial } from '@/api/queries'
import type { LicenseInfo } from '@/api/types'
import { BusinessBadge } from '@/components/BusinessBadge'
import { Button } from '@/components/Button'
import { ErrorState } from '@/components/ErrorState'
import { licenceNotice } from '@/components/LicenseBanner'
import { Spinner } from '@/components/Spinner'
import { LicenceServerSection } from '@/pages/LicenceServerSection'
import { SeatReportSection } from '@/pages/SeatReportSection'
import { UsageNoticesSection } from '@/pages/UsageNoticesSection'
import i18n, { formatDateOnly } from '@/i18n'

const PRICING_URL = 'https://annotide.com/pricing/'

const INPUT_CLASS =
  'rounded-md border border-line bg-surface px-2 py-1.5 font-mono text-xs text-ink ' +
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent'

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? (error.detail ?? error.title) : i18n.t('admin:errors.unknown')
}

function seatsText(t: TFunction<['admin', 'common']>, info: LicenseInfo): string {
  if (info.seat_limit === null) {
    return t('licence.seats.noLimit', { active: info.active_users })
  }
  if (info.seats === null) {
    return t('licence.seats.communityOfThree', { active: info.active_users })
  }
  const overage = info.seat_limit - info.seats
  return t('licence.seats.withOverage', { active: info.active_users, seats: info.seats, overage })
}

function Details({ info }: { info: LicenseInfo }): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const rows: Array<[string, string]> = [
    [t('licence.details.rows.edition'), t(`licence.editions.${info.tier}`, { defaultValue: info.tier })],
    [t('licence.details.rows.licensee'), info.licensee ?? '—'],
    [t('licence.details.rows.seatsInUse'), seatsText(t, info)],
    [
      t('licence.details.rows.expires'),
      info.expires_at ? formatDateOnly(`${info.expires_at}T00:00:00Z`) : '—',
    ],
    [
      t('licence.details.rows.graceEnds'),
      info.grace_ends_at ? formatDateOnly(`${info.grace_ends_at}T00:00:00Z`) : '—',
    ],
    [
      t('licence.details.rows.boundTo'),
      info.license_id === null
        ? '—'
        : info.hosts.length > 0
          ? info.hosts.join(', ')
          : t('licence.details.anyHost'),
    ],
    [
      t('licence.details.rows.revoked'),
      info.revoked_at ? formatDateOnly(`${info.revoked_at}T00:00:00Z`) : '—',
    ],
    [
      t('licence.details.rows.keyFrom'),
      info.source ? t(`licence.details.source.${info.source}`) : '—',
    ],
    [t('licence.details.rows.licenceId'), info.license_id ?? '—'],
  ]
  return (
    <section aria-label={t('licence.details.ariaLabel')} className="mb-8 rounded-lg border border-line p-4">
      <div className="mb-3 flex items-center gap-2">
        <h2 className="text-base font-semibold text-ink">{t('licence.details.heading')}</h2>
        <span className={`rounded-full px-2 py-0.5 text-xs ${STATUS_CLASS[info.status]}`}>
          {t(`licence.details.status.${info.status}`)}
        </span>
      </div>
      <dl className="grid gap-x-6 gap-y-2 text-sm sm:grid-cols-[max-content_1fr]">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-muted">{label}</dt>
            <dd className="break-all text-ink">{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}

const STATUS_CLASS: Record<LicenseInfo['status'], string> = {
  community: 'bg-line/60 text-muted',
  valid: 'bg-green-600/15 text-green-800 dark:text-success',
  expired: 'bg-red-500/15 text-red-700 dark:text-danger',
  invalid: 'bg-red-500/15 text-red-700 dark:text-danger',
}

/** Where the install stands and the next step (LIC-35): usage, the trial, the Business features. */
function Edition({ info }: { info: LicenseInfo }): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const trial = useStartTrial()
  const community = info.tier === 'community'
  const licensed = new Set(info.features)
  const expires = info.expires_at ? formatDateOnly(`${info.expires_at}T00:00:00Z`) : ''

  return (
    <section aria-label={t('licence.edition.ariaLabel')} className="mb-8 rounded-lg border border-line p-4">
      <h2 className="mb-1 text-base font-semibold text-ink">
        {t(`licence.editions.${info.tier}`, { defaultValue: info.tier })}
      </h2>
      {community && (
        <>
          <p className="mb-1 text-sm text-ink">
            {t('licence.edition.communityUsage', { active: info.active_users })}
          </p>
          <p className="mb-3 max-w-prose text-sm text-muted">
            {t('licence.edition.communityNext')}{' '}
            <a href={PRICING_URL} target="_blank" rel="noreferrer" className="text-accent underline">
              {t('licence.edition.pricing')}
            </a>
          </p>
          {info.trial_used ? (
            <p className="mb-3 text-sm text-muted">{t('licence.edition.trialUsed')}</p>
          ) : (
            <div className="mb-3">
              <Button onClick={() => trial.mutate()} disabled={trial.isPending}>
                {trial.isPending ? t('licence.edition.trialStarting') : t('licence.edition.startTrial')}
              </Button>
              <p className="mt-2 max-w-prose text-xs text-muted">{t('licence.edition.trialPrivacy')}</p>
            </div>
          )}
          {trial.isError && (
            <p role="alert" className="mb-3 text-sm text-danger">
              {errorMessage(trial.error)}
            </p>
          )}
        </>
      )}
      {info.tier === 'trial' && (
        <p className="mb-3 max-w-prose text-sm text-ink">
          {t('licence.edition.trialUntil', { date: expires })}
        </p>
      )}
      <h3 className="mb-2 mt-2 text-sm font-semibold text-ink">{t('licence.edition.featuresHeading')}</h3>
      <ul className="grid gap-1 text-sm sm:grid-cols-2">
        {info.business_features.map((feature) => (
          <li key={feature} className="flex items-center gap-2">
            <span className="text-ink">{t(`licence.features.${feature}`)}</span>
            {licensed.has(feature) ? (
              <span className="text-xs text-success">{t('licence.edition.included')}</span>
            ) : (
              <BusinessBadge />
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}

/** The installation's licence: what is in force, seats in use, install or remove a key. */
export function LicensePage(): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const licence = useLicense()
  const install = useInstallLicense()
  const remove = useRemoveLicense()
  const [key, setKey] = useState('')
  const [confirmingRemove, setConfirmingRemove] = useState(false)

  const handleSubmit = (event: FormEvent): void => {
    event.preventDefault()
    install.mutate({ key }, { onSuccess: () => setKey('') })
  }

  const info = licence.data
  const notice = info ? licenceNotice(info) : null
  const storedKeyInForce = info?.source === 'admin' || info?.source === 'refresh'

  return (
    <div className="mx-auto w-full max-w-3xl flex-1 px-4 py-8 sm:px-6">
      <h1 className="mb-2 text-xl font-semibold text-ink">{t('licence.page.title')}</h1>
      <p className="mb-6 max-w-prose text-sm text-muted">
        <Trans
          t={t}
          i18nKey="licence.page.description"
          components={{ code: <code className="font-mono text-xs" /> }}
        />
      </p>

      {licence.isLoading && (
        <div className="flex justify-center py-16">
          <Spinner label={t('licence.page.loading')} />
        </div>
      )}
      {licence.isError && (
        <ErrorState
          title={t('licence.page.loadError')}
          message={errorMessage(licence.error)}
          onRetry={() => void licence.refetch()}
        />
      )}

      {info && (
        <>
          {notice && (
            <p role="alert" className="mb-4 text-sm text-danger">
              {notice.text}
            </p>
          )}
          <Edition info={info} />
          <Details info={info} />
          <UsageNoticesSection />
          <SeatReportSection />
          <LicenceServerSection />

          <form
            onSubmit={handleSubmit}
            aria-label={t('licence.page.installTitle')}
            className="mb-6 rounded-lg border border-line p-4"
          >
            <h2 className="mb-3 text-base font-semibold text-ink">{t('licence.page.installTitle')}</h2>
            <label className="flex flex-col gap-1 text-sm text-ink">
              {t('licence.page.keyLabel')}
              <textarea
                className={INPUT_CLASS}
                rows={4}
                value={key}
                spellCheck={false}
                placeholder="ANN1.…"
                onChange={(event) => setKey(event.target.value)}
              />
            </label>
            {install.isError && (
              <p role="alert" className="mt-3 text-sm text-danger">
                {errorMessage(install.error)}
              </p>
            )}
            {install.isSuccess && !install.isPending && (
              <p className="mt-3 text-sm text-success">
                {t('licence.page.installed')}
              </p>
            )}
            <div className="mt-3">
              <Button type="submit" disabled={key.trim() === '' || install.isPending}>
                {t('licence.page.install')}
              </Button>
            </div>
          </form>

          {storedKeyInForce && (
            <div className="flex flex-wrap items-center gap-3 text-sm">
              {confirmingRemove ? (
                <>
                  <span className="text-ink">
                    <Trans
                      t={t}
                      i18nKey="licence.page.removeConfirm"
                      components={{ code: <code className="font-mono text-xs" /> }}
                    />
                  </span>
                  <Button
                    size="sm"
                    variant="danger"
                    disabled={remove.isPending}
                    onClick={() =>
                      remove.mutate(undefined, { onSettled: () => setConfirmingRemove(false) })
                    }
                  >
                    {t('licence.page.remove')}
                  </Button>
                  <Button size="sm" variant="ghost" onClick={() => setConfirmingRemove(false)}>
                    {t('common:cancel')}
                  </Button>
                </>
              ) : (
                <Button size="sm" variant="secondary" onClick={() => setConfirmingRemove(true)}>
                  {t('licence.page.removeStored')}
                </Button>
              )}
            </div>
          )}
          {remove.isError && (
            <p role="alert" className="mt-3 text-sm text-danger">
              {t('licence.page.removeError', { message: errorMessage(remove.error) })}
            </p>
          )}
        </>
      )}
    </div>
  )
}
