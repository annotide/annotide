import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '@/api/client'
import {
  useDisableMfa,
  useEnableMfa,
  useExportPersonalData,
  useMe,
  useMfa,
  useReplaceRecoveryCodes,
  useSetupMfa,
  useUpdateMe,
} from '@/api/queries'
import type { MfaSetup } from '@/api/types'
import { Button } from '@/components/Button'
import { ErrorState } from '@/components/ErrorState'
import { QrCode } from '@/components/QrCode'
import { Spinner } from '@/components/Spinner'
import { useAuthStore } from '@/lib/store'
import { isMfaSetupToken } from '@/lib/token'
import i18n from '@/i18n'

const INPUT_CLASS =
  'w-40 rounded-md border border-line bg-surface px-2 py-1.5 font-mono text-sm text-ink ' +
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent'

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? (error.detail ?? error.title) : i18n.t('admin:errors.unknown')
}

function RecoveryCodes({ codes }: { codes: string[] }): JSX.Element {
  const { t } = useTranslation('admin')
  return (
    <div className="mt-4 rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
      <p className="mb-2 font-medium text-ink">{t('security.recoveryCodes.title')}</p>
      <p className="mb-2 text-muted">{t('security.recoveryCodes.hint')}</p>
      <ul
        aria-label={t('security.recoveryCodes.ariaLabel')}
        className="grid grid-cols-2 gap-1 font-mono text-ink"
      >
        {codes.map((code) => (
          <li key={code}>{code}</li>
        ))}
      </ul>
    </div>
  )
}

/** One code field and a button: every MFA step here needs the same thing. */
function CodeForm({
  label,
  action,
  pending,
  onSubmit,
  variant = 'primary',
}: {
  label: string
  action: string
  pending: boolean
  onSubmit: (code: string) => void
  variant?: 'primary' | 'secondary' | 'danger'
}): JSX.Element {
  const [code, setCode] = useState('')
  const submit = (event: FormEvent): void => {
    event.preventDefault()
    onSubmit(code.trim())
    setCode('')
  }
  return (
    <form onSubmit={submit} className="flex flex-wrap items-end gap-2">
      <label className="flex flex-col gap-1 text-sm text-ink">
        {label}
        <input
          className={INPUT_CLASS}
          inputMode="numeric"
          autoComplete="one-time-code"
          value={code}
          onChange={(event) => setCode(event.target.value)}
        />
      </label>
      <Button type="submit" size="sm" variant={variant} disabled={pending || code.trim() === ''}>
        {action}
      </Button>
    </form>
  )
}

function saveJson(data: unknown, filename: string): void {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  link.click()
  URL.revokeObjectURL(url)
}

/** E-mail copies of mentions, replies and review verdicts (API-7). */
function NotificationsSection(): JSX.Element | null {
  const { t } = useTranslation('admin')
  const me = useMe(true)
  const update = useUpdateMe()
  if (!me.data) return null
  const checked = me.data.email_notifications ?? true
  return (
    <section
      aria-label={t('security.notifications.ariaLabel')}
      className="mt-6 rounded-lg border border-line p-4 text-sm"
    >
      <h2 className="mb-2 text-base font-semibold text-ink">
        {t('security.notifications.heading')}
      </h2>
      <p className="mb-3 max-w-prose text-muted">{t('security.notifications.description')}</p>
      <label className="flex items-center gap-2 text-ink">
        <input
          type="checkbox"
          checked={checked}
          disabled={update.isPending}
          onChange={(event) => update.mutate({ email_notifications: event.target.checked })}
        />
        {t('security.notifications.email')}
      </label>
      {update.isError && (
        <p role="alert" className="mt-3 text-danger">
          {errorMessage(update.error)}
        </p>
      )}
    </section>
  )
}

function PersonalDataSection(): JSX.Element | null {
  const { t } = useTranslation('admin')
  const user = useAuthStore((s) => s.user)
  const exportData = useExportPersonalData()
  if (!user) return null
  return (
    <section
      aria-label={t('security.personalData.ariaLabel')}
      className="mt-6 rounded-lg border border-line p-4 text-sm"
    >
      <h2 className="mb-2 text-base font-semibold text-ink">{t('security.personalData.heading')}</h2>
      <p className="mb-3 max-w-prose text-muted">{t('security.personalData.description')}</p>
      <Button
        variant="secondary"
        disabled={exportData.isPending}
        onClick={() =>
          exportData.mutate(user.id, {
            onSuccess: (data) => saveJson(data, `personal-data-${user.id}.json`),
          })
        }
      >
        {exportData.isPending ? t('security.personalData.preparing') : t('security.personalData.download')}
      </Button>
      {exportData.isError && (
        <p role="alert" className="mt-3 text-danger">
          {errorMessage(exportData.error)}
        </p>
      )}
    </section>
  )
}

export function SecurityPage(): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const status = useMfa()
  const setup = useSetupMfa()
  const enable = useEnableMfa()
  const replace = useReplaceRecoveryCodes()
  const disable = useDisableMfa()
  const [seed, setSeed] = useState<MfaSetup | null>(null)
  const [codes, setCodes] = useState<string[] | null>(null)

  const data = status.data
  const failure = [setup, enable, replace, disable].find((m) => m.isError)?.error
  // AUTH-2 policy: this administrator's token only reaches the MFA routes.
  const setupOnly = isMfaSetupToken(useAuthStore((s) => s.token))
  const logout = useAuthStore((s) => s.logout)

  return (
    <div className="mx-auto w-full max-w-3xl flex-1 px-4 py-8 sm:px-6">
      <h1 className="mb-2 text-xl font-semibold text-ink">{t('security.title')}</h1>
      <p className="mb-6 max-w-prose text-sm text-muted">{t('security.description')}</p>
      {setupOnly && (
        <div role="status" className="mb-6 rounded-lg border border-accent/40 p-4 text-sm text-ink">
          <p>{data?.enabled ? t('security.mfaRequired.done') : t('security.mfaRequired.notice')}</p>
          {data?.enabled && (
            <Button className="mt-3" onClick={logout}>
              {t('security.mfaRequired.signInAgain')}
            </Button>
          )}
        </div>
      )}

      {status.isLoading && <Spinner label={t('common:loading')} />}
      {status.isError && (
        <ErrorState
          title={t('security.loadError')}
          message={errorMessage(status.error)}
          onRetry={() => void status.refetch()}
        />
      )}

      {data && (
        <section
          aria-label={t('security.mfa.ariaLabel')}
          className="rounded-lg border border-line p-4 text-sm"
        >
          <h2 className="mb-2 text-base font-semibold text-ink">{t('security.mfa.heading')}</h2>

          {!data.available && <p className="text-ink">{t('security.mfa.ssoNotice')}</p>}

          {data.available && !data.enabled && !seed && (
            <>
              <p className="mb-3 text-ink">{t('security.mfa.off')}</p>
              <Button
                size="sm"
                disabled={setup.isPending}
                onClick={() => setup.mutate(undefined, { onSuccess: setSeed })}
              >
                {t('security.mfa.setup')}
              </Button>
            </>
          )}

          {data.available && !data.enabled && seed && (
            <>
              <p className="mb-2 text-ink">{t('security.mfa.scanInstructions')}</p>
              <div className="mb-3">
                <QrCode value={seed.otpauth_uri} label={t('security.mfa.qrLabel')} />
              </div>
              <dl className="mb-3 grid gap-1 sm:grid-cols-[max-content_1fr] sm:gap-x-4">
                <dt className="text-muted">{t('security.mfa.key')}</dt>
                <dd className="break-all font-mono text-ink">{seed.secret}</dd>
                <dt className="text-muted">{t('security.mfa.link')}</dt>
                <dd className="break-all">
                  <a className="text-accent underline" href={seed.otpauth_uri}>
                    {seed.otpauth_uri}
                  </a>
                </dd>
              </dl>
              <CodeForm
                label={t('security.mfa.codeFromApp')}
                action={t('security.mfa.turnOn')}
                pending={enable.isPending}
                onSubmit={(code) =>
                  enable.mutate(code, {
                    onSuccess: (result) => {
                      setSeed(null)
                      setCodes(result.recovery_codes)
                    },
                  })
                }
              />
            </>
          )}

          {data.enabled && (
            <>
              <p className="mb-3 text-ink">
                {t('security.mfa.onStatus', { count: data.recovery_codes_left })}
              </p>
              <div className="flex flex-col gap-4">
                <CodeForm
                  label={t('security.mfa.codeFromApp')}
                  action={t('security.mfa.newRecoveryCodes')}
                  variant="secondary"
                  pending={replace.isPending}
                  onSubmit={(code) =>
                    replace.mutate(code, { onSuccess: (r) => setCodes(r.recovery_codes) })
                  }
                />
                <CodeForm
                  label={t('security.mfa.codeOrRecovery')}
                  action={t('security.mfa.turnOff')}
                  variant="danger"
                  pending={disable.isPending}
                  onSubmit={(code) => disable.mutate(code, { onSuccess: () => setCodes(null) })}
                />
              </div>
            </>
          )}

          {failure !== undefined && failure !== null && (
            <p role="alert" className="mt-3 text-danger">
              {errorMessage(failure)}
            </p>
          )}
          {codes && <RecoveryCodes codes={codes} />}
        </section>
      )}

      {!setupOnly && (
        <>
          <NotificationsSection />
          <PersonalDataSection />
        </>
      )}
    </div>
  )
}
