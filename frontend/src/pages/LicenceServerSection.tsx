import { Trans, useTranslation } from 'react-i18next'

import { ApiError } from '@/api/client'
import { useLicenseRefresh, useRefreshLicenseNow, useTelemetryPreview } from '@/api/queries'
import { Button } from '@/components/Button'
import i18n, { formatDateTime } from '@/i18n'

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? (error.detail ?? error.title) : i18n.t('admin:errors.unknown')
}

function when(value: string | null): string {
  return value ? formatDateTime(value) : i18n.t('common:never')
}

function Payload({ label, body }: { label: string; body: unknown }): JSX.Element {
  return (
    <details className="mt-2 text-sm">
      <summary className="cursor-pointer text-muted">{label}</summary>
      <pre className="mt-1 overflow-x-auto rounded-md bg-line/30 p-2 font-mono text-xs text-ink">
        {JSON.stringify(body, null, 2)}
      </pre>
    </details>
  )
}

function RefreshStatus(): JSX.Element | null {
  const { t } = useTranslation(['admin', 'common'])
  const status = useLicenseRefresh()
  const refreshNow = useRefreshLicenseNow()
  const data = status.data
  if (status.isError) {
    return (
      <p role="alert" className="text-sm text-danger">
        {t('licence.server.refresh.loadError', { message: errorMessage(status.error) })}
      </p>
    )
  }
  if (!data) return null

  const state = !data.server_configured
    ? t('licence.server.refresh.stateNoServer')
    : !data.enabled
      ? t('licence.server.refresh.stateDisabled')
      : data.payload === null
        ? t('licence.server.refresh.stateIdle')
        : t('licence.server.refresh.stateOn')
  return (
    <div className="mb-4">
      <h3 className="text-sm font-semibold text-ink">{t('licence.server.refresh.heading')}</h3>
      <p className="text-sm text-muted">
        {t('licence.server.refresh.description', { state })}
      </p>
      <p className="mt-1 text-sm text-ink">
        {t('licence.server.refresh.lastStatus', {
          succeeded: when(data.succeeded_at),
          attempted: when(data.attempted_at),
        })}
      </p>
      {data.error && (
        <p role="alert" className="mt-1 text-sm text-danger">
          {t('licence.server.refresh.failed', { error: data.error })}
        </p>
      )}
      {refreshNow.isError && (
        <p role="alert" className="mt-1 text-sm text-danger">
          {errorMessage(refreshNow.error)}
        </p>
      )}
      {data.payload && <Payload label={t('licence.server.refresh.payloadWhatSent')} body={data.payload} />}
      {data.last_payload && (
        <Payload label={t('licence.server.refresh.payloadLastSent')} body={data.last_payload} />
      )}
      <div className="mt-2">
        <Button
          size="sm"
          variant="secondary"
          disabled={
            refreshNow.isPending || !data.enabled || !data.server_configured || !data.payload
          }
          onClick={() => refreshNow.mutate()}
        >
          {t('licence.server.refresh.refreshNow')}
        </Button>
      </div>
    </div>
  )
}

function HeartbeatStatus(): JSX.Element | null {
  const { t } = useTranslation(['admin', 'common'])
  const preview = useTelemetryPreview()
  const data = preview.data
  if (preview.isError) {
    return (
      <p role="alert" className="text-sm text-danger">
        {t('licence.server.heartbeat.loadError', { message: errorMessage(preview.error) })}
      </p>
    )
  }
  if (!data) return null

  const { enabled, server_configured, attempted_at, sent_at, error, last_payload } = data
  const payload = {
    install_id: data.install_id,
    version: data.version,
    licence_type: data.licence_type,
    active_users: data.active_users,
    fingerprint: data.fingerprint,
  }
  const state = !enabled
    ? t('licence.server.heartbeat.stateOff')
    : server_configured
      ? t('licence.server.heartbeat.stateOn')
      : t('licence.server.heartbeat.stateNoServer')
  return (
    <div>
      <h3 className="text-sm font-semibold text-ink">{t('licence.server.heartbeat.heading')}</h3>
      <p className="text-sm text-muted">
        <Trans t={t} i18nKey="licence.server.heartbeat.description" values={{ state }} components={{ code: <code className="font-mono text-xs" /> }} />
      </p>
      {data.notice && <p className="mt-1 text-sm text-muted">{data.notice}</p>}
      {enabled && (
        <p className="mt-1 text-sm text-ink">
          {t('licence.server.heartbeat.lastStatus', {
            sent: when(sent_at),
            attempted: when(attempted_at),
          })}
        </p>
      )}
      {error && (
        <p role="alert" className="mt-1 text-sm text-danger">
          {t('licence.server.heartbeat.failed', { error })}
        </p>
      )}
      <Payload label={t('licence.server.heartbeat.payloadWouldSend')} body={payload} />
      {data.withheld.length > 0 && (
        <details className="mt-2 text-sm">
          <summary className="cursor-pointer text-muted">{t('licence.server.heartbeat.withheld')}</summary>
          <ul className="mt-1 list-disc pl-5 text-xs text-ink">
            {data.withheld.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </details>
      )}
      {last_payload && (
        <Payload label={t('licence.server.heartbeat.payloadLastSent')} body={last_payload} />
      )}
    </div>
  )
}

/** What this install tells the licence server, and when it last did (LIC-6, LIC-21, LIC-27). */
export function LicenceServerSection(): JSX.Element {
  const { t } = useTranslation('admin')
  return (
    <section aria-label={t('licence.server.sectionAriaLabel')} className="mb-8 rounded-lg border border-line p-4">
      <h2 className="mb-3 text-base font-semibold text-ink">{t('licence.server.heading')}</h2>
      <RefreshStatus />
      <HeartbeatStatus />
    </section>
  )
}
