import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import type { TFunction } from 'i18next'
import type { ApiKey, ApiKeyCreate, ApiKeyCreated, ApiKeyScope } from '@/api/types'
import { ApiError } from '@/api/client'
import { Button } from '@/components/Button'
import { formatDateTime } from '@/i18n'
import i18n from '@/i18n'

// Shared by the personal keys page and the service accounts panel (AUTH-4).

function scopeOptions(
  t: TFunction<['admin', 'common']>,
): Array<{ value: ApiKeyScope; label: string; hint: string }> {
  return [
    { value: 'read', label: t('apiKeys.scopes.read.label'), hint: t('apiKeys.scopes.read.hint') },
    { value: 'write', label: t('apiKeys.scopes.write.label'), hint: t('apiKeys.scopes.write.hint') },
    { value: 'admin', label: t('apiKeys.scopes.admin.label'), hint: t('apiKeys.scopes.admin.hint') },
  ]
}

export const INPUT_CLASS =
  'rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink ' +
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent'

export function errorMessage(error: unknown): string {
  return error instanceof ApiError ? (error.detail ?? error.title) : i18n.t('admin:errors.unknown')
}

/** Locale-aware; `—` when there is no value. */
export function formatDate(value: string | null): string {
  return value ? formatDateTime(value) : '—'
}

type KeyStatus = 'active' | 'expired' | 'revoked'

function keyStatus(key: ApiKey, now: number): KeyStatus {
  if (key.revoked_at) return 'revoked'
  if (key.expires_at && new Date(key.expires_at).getTime() <= now) return 'expired'
  return 'active'
}

const STATUS_CLASS: Record<KeyStatus, string> = {
  active: 'bg-green-600/15 text-success',
  expired: 'bg-amber-500/15 text-amber-700 dark:text-amber-400',
  revoked: 'bg-line/60 text-muted',
}

/** Default expiry offered in the form: 90 days out, at local midnight. */
function defaultExpiryDate(): string {
  const date = new Date()
  date.setDate(date.getDate() + 90)
  return date.toISOString().slice(0, 10)
}

interface KeyFormValue {
  name: string
  scopes: ApiKeyScope[]
  expires: string
}

interface KeyFormProps {
  /** Heading and accessible name of the form. */
  title: string
  /** Keeps element ids unique when several forms are on the page. */
  idPrefix: string
  pending: boolean
  errorText: string | null
  onSubmit: (payload: ApiKeyCreate) => void
}

export function KeyForm({
  title,
  idPrefix,
  pending,
  errorText,
  onSubmit,
}: KeyFormProps): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const [value, setValue] = useState<KeyFormValue>({
    name: '',
    scopes: ['read'],
    expires: defaultExpiryDate(),
  })
  const expiresId = `${idPrefix}-expires`
  const options = scopeOptions(t)

  function toggleScope(scope: ApiKeyScope): void {
    setValue((current) => ({
      ...current,
      scopes: current.scopes.includes(scope)
        ? current.scopes.filter((item) => item !== scope)
        : [...current.scopes, scope],
    }))
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault()
    // End of the chosen day, local time, so "expires 30 June" means all of 30 June.
    const expiresAt = value.expires ? new Date(`${value.expires}T23:59:59`).toISOString() : null
    onSubmit({ name: value.name.trim(), scopes: value.scopes, expires_at: expiresAt })
  }

  const canSubmit = value.name.trim() !== '' && value.scopes.length > 0 && !pending

  return (
    <form
      onSubmit={handleSubmit}
      aria-label={title}
      className="mb-8 rounded-lg border border-line bg-surface p-4"
    >
      <h2 className="mb-3 text-base font-semibold text-ink">{title}</h2>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-sm text-ink">
          {t('apiKeys.form.name')}
          <input
            className={INPUT_CLASS}
            value={value.name}
            maxLength={255}
            placeholder={t('apiKeys.form.namePlaceholder')}
            onChange={(event) => setValue({ ...value, name: event.target.value })}
            required
          />
        </label>
        <div className="flex flex-col gap-1 text-sm text-ink">
          <label htmlFor={expiresId}>{t('apiKeys.form.expiresOn')}</label>
          <input
            id={expiresId}
            type="date"
            className={INPUT_CLASS}
            value={value.expires}
            aria-describedby={`${expiresId}-hint`}
            onChange={(event) => setValue({ ...value, expires: event.target.value })}
          />
          <span id={`${expiresId}-hint`} className="text-xs text-muted">
            {t('apiKeys.form.expiresHint')}
          </span>
        </div>
      </div>
      <fieldset className="mt-3">
        <legend className="mb-1 text-sm text-ink">{t('apiKeys.form.scopesLegend')}</legend>
        <div className="flex flex-wrap gap-4">
          {options.map((option) => (
            <label key={option.value} className="flex items-start gap-2 text-sm text-ink">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={value.scopes.includes(option.value)}
                onChange={() => toggleScope(option.value)}
              />
              <span>
                {option.label}
                <span className="block text-xs text-muted">{option.hint}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>
      {errorText && (
        <p role="alert" className="mt-3 text-sm text-danger">
          {errorText}
        </p>
      )}
      <div className="mt-4">
        <Button type="submit" disabled={!canSubmit}>
          {pending ? t('apiKeys.form.creating') : t('apiKeys.form.submit')}
        </Button>
      </div>
    </form>
  )
}

interface NewKeyBannerProps {
  created: ApiKeyCreated
  onDismiss: () => void
}

/** Shows the token once. Nothing else ever displays it again. */
export function NewKeyBanner({ created, onDismiss }: NewKeyBannerProps): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const [copied, setCopied] = useState(false)

  async function copy(): Promise<void> {
    try {
      await navigator.clipboard.writeText(created.token)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  return (
    <div
      role="status"
      className="mb-8 rounded-lg border border-accent/60 bg-accent/5 p-4 text-sm text-ink"
    >
      <p className="font-semibold">{t('apiKeys.newKeyBanner.title', { name: created.name })}</p>
      <p className="mt-1 text-muted">{t('apiKeys.newKeyBanner.copyNotice')}</p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <code
          aria-label={t('apiKeys.newKeyBanner.tokenAriaLabel')}
          className="select-all break-all rounded-md border border-line bg-surface px-2 py-1 font-mono text-xs"
        >
          {created.token}
        </code>
        <Button size="sm" variant="secondary" onClick={() => void copy()}>
          {copied ? t('apiKeys.newKeyBanner.copied') : t('apiKeys.newKeyBanner.copy')}
        </Button>
        <Button size="sm" variant="ghost" onClick={onDismiss}>
          {t('apiKeys.newKeyBanner.dismiss')}
        </Button>
      </div>
    </div>
  )
}

interface KeyRowProps {
  apiKey: ApiKey
  now: number
  revoking: boolean
  onRevoke: () => void
}

function KeyRow({ apiKey, now, revoking, onRevoke }: KeyRowProps): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const status = keyStatus(apiKey, now)
  return (
    <tr className="border-b border-line last:border-b-0">
      <td className="px-3 py-2 text-sm text-ink">{apiKey.name}</td>
      <td className="px-3 py-2 text-sm text-ink">{apiKey.scopes.join(', ')}</td>
      <td className="px-3 py-2">
        <span
          className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_CLASS[status]}`}
        >
          {t(`apiKeys.status.${status}`)}
        </span>
      </td>
      <td className="px-3 py-2 text-sm text-muted">{formatDate(apiKey.created_at)}</td>
      <td className="px-3 py-2 text-sm text-muted">{formatDate(apiKey.last_used_at)}</td>
      <td className="px-3 py-2 text-sm text-muted">{formatDate(apiKey.expires_at)}</td>
      <td className="px-3 py-2">
        {status !== 'revoked' && (
          <Button
            size="sm"
            variant="danger"
            disabled={revoking}
            aria-label={t('apiKeys.row.revokeAriaLabel', { name: apiKey.name })}
            onClick={onRevoke}
          >
            {t('apiKeys.row.revoke')}
          </Button>
        )}
      </td>
    </tr>
  )
}

interface KeysTableProps {
  keys: ApiKey[]
  /** Id of the key whose revoke request is in flight. */
  revokingId: string | null
  /** Called on the confirming second click only. */
  onRevoke: (key: ApiKey) => void
}

/** Key list with a two-click revoke. */
export function KeysTable({ keys, revokingId, onRevoke }: KeysTableProps): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const [confirmingId, setConfirmingId] = useState<string | null>(null)
  const now = Date.now()

  function handleRevoke(key: ApiKey): void {
    if (confirmingId !== key.id) {
      setConfirmingId(key.id)
      return
    }
    setConfirmingId(null)
    onRevoke(key)
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-line">
      <table className="w-full text-left">
        <thead>
          <tr className="border-b border-line bg-surface">
            <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
              {t('apiKeys.table.name')}
            </th>
            <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
              {t('apiKeys.table.scopes')}
            </th>
            <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
              {t('apiKeys.table.status')}
            </th>
            <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
              {t('apiKeys.table.created')}
            </th>
            <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
              {t('apiKeys.table.lastUsed')}
            </th>
            <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
              {t('apiKeys.table.expires')}
            </th>
            <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
              {t('apiKeys.table.actions')}
            </th>
          </tr>
        </thead>
        <tbody>
          {keys.map((apiKey) => (
            <KeyRow
              key={apiKey.id}
              apiKey={apiKey}
              now={now}
              revoking={revokingId === apiKey.id}
              onRevoke={() => handleRevoke(apiKey)}
            />
          ))}
        </tbody>
      </table>
      {confirmingId && (
        <p role="status" className="border-t border-line px-3 py-2 text-sm text-muted">
          {t('apiKeys.confirmRevoke')}
        </p>
      )}
    </div>
  )
}
