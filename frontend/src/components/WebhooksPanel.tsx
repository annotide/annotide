import { useState } from 'react'
import { Trans, useTranslation } from 'react-i18next'
import {
  useCreateWebhook,
  useDeleteWebhook,
  useTestWebhook,
  useUpdateWebhook,
  useWebhookDeliveries,
  useWebhooks,
} from '@/api/queries'
import { ApiError } from '@/api/client'
import {
  WEBHOOK_EVENTS,
  type Webhook,
  type WebhookDelivery,
  type WebhookFormat,
} from '@/api/types'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { ErrorState } from '@/components/ErrorState'

export interface WebhooksPanelProps {
  /** Project-scoped hooks when set; organisation-wide hooks (superuser) when omitted. */
  projectId?: string
  /** Hide the write controls (viewer of the page without the right role). */
  readOnly?: boolean
}

function errorText(error: unknown, fallback: string): string {
  return error instanceof ApiError ? (error.detail ?? error.title) : fallback
}

/** Mask the one-time secret display so a glance over the shoulder shows little. */
function SecretReveal({ secret, onDismiss }: { secret: string; onDismiss: () => void }) {
  const { t } = useTranslation(['admin', 'common'])
  const [shown, setShown] = useState(false)
  return (
    <div
      role="status"
      className="mb-3 rounded-md border border-amber-400/60 bg-amber-500/10 p-3 text-sm text-ink"
    >
      <p className="mb-1 font-medium">{t('webhooks.panel.secretReveal.title')}</p>
      <code className="block break-all font-mono text-xs" data-testid="webhook-secret">
        {shown ? secret : '•'.repeat(24)}
      </code>
      <div className="mt-2 flex gap-2">
        <Button variant="secondary" size="sm" onClick={() => setShown((v) => !v)}>
          {shown ? t('webhooks.panel.secretReveal.hide') : t('webhooks.panel.secretReveal.reveal')}
        </Button>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => {
            void navigator.clipboard?.writeText(secret)
          }}
        >
          {t('webhooks.panel.secretReveal.copy')}
        </Button>
        <Button variant="ghost" size="sm" onClick={onDismiss}>
          {t('webhooks.panel.secretReveal.done')}
        </Button>
      </div>
    </div>
  )
}

function DeliveryLog({ webhookId }: { webhookId: string }) {
  const { t } = useTranslation(['admin', 'common'])
  const deliveriesQuery = useWebhookDeliveries(webhookId)
  const rows: WebhookDelivery[] = deliveriesQuery.data?.items ?? []
  if (deliveriesQuery.isLoading) return <Spinner label={t('webhooks.panel.deliveryLog.loading')} />
  if (deliveriesQuery.isError) {
    return (
      <p className="text-xs text-danger" role="alert">
        {errorText(deliveriesQuery.error, t('webhooks.panel.deliveryLog.loadError'))}
      </p>
    )
  }
  if (rows.length === 0) return <p className="text-xs text-muted">{t('webhooks.panel.deliveryLog.empty')}</p>
  return (
    <table className="w-full text-left text-xs" aria-label={t('webhooks.panel.deliveryLog.ariaLabel')}>
      <thead>
        <tr className="text-muted">
          <th className="py-1 pr-2 font-medium">{t('webhooks.panel.deliveryLog.columnEvent')}</th>
          <th className="py-1 pr-2 font-medium">{t('webhooks.panel.deliveryLog.columnStatus')}</th>
          <th className="py-1 pr-2 font-medium">{t('webhooks.panel.deliveryLog.columnAttempts')}</th>
          <th className="py-1 pr-2 font-medium">{t('webhooks.panel.deliveryLog.columnResponse')}</th>
          <th className="py-1 font-medium">{t('webhooks.panel.deliveryLog.columnWhen')}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((d) => (
          <tr key={d.id} className="border-t border-line/50">
            <td className="py-1 pr-2 font-mono text-ink">{d.event}</td>
            <td
              className={`py-1 pr-2 ${
                d.status === 'succeeded'
                  ? 'text-success'
                  : d.status === 'failed'
                    ? 'text-danger'
                    : 'text-muted'
              }`}
            >
              {d.status}
            </td>
            <td className="py-1 pr-2 text-ink">{d.attempts}</td>
            <td className="py-1 pr-2 text-ink" title={d.error ?? undefined}>
              {d.response_status ?? d.error ?? '—'}
            </td>
            <td className="py-1 text-muted">
              {new Date(d.delivered_at ?? d.next_attempt_at).toLocaleString()}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** Outbound webhooks (API-4): subscribe a URL to events, see deliveries,
 * send a test, rotate the secret, pause or remove. */
export function WebhooksPanel({ projectId, readOnly = false }: WebhooksPanelProps): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const hooksQuery = useWebhooks(projectId)
  const createHook = useCreateWebhook(projectId)
  const updateHook = useUpdateWebhook(projectId)
  const deleteHook = useDeleteWebhook(projectId)
  const testHook = useTestWebhook()

  const [url, setUrl] = useState('')
  const [description, setDescription] = useState('')
  const [events, setEvents] = useState<string[]>(['*'])
  const [format, setFormat] = useState<WebhookFormat>('json')
  const [formError, setFormError] = useState<string | null>(null)
  const [secret, setSecret] = useState<string | null>(null)
  const [openLog, setOpenLog] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null)
  const [rowError, setRowError] = useState<string | null>(null)
  const [tested, setTested] = useState<string | null>(null)

  const hooks: Webhook[] = hooksQuery.data?.items ?? []
  const inputClass =
    'rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent'

  function toggleEvent(name: string): void {
    setEvents((prev) => {
      if (name === '*') return prev.includes('*') ? [] : ['*']
      const without = prev.filter((e) => e !== '*' && e !== name)
      return prev.includes(name) ? without : [...without, name]
    })
  }

  function handleCreate(): void {
    if (!/^https?:\/\//.test(url.trim())) {
      setFormError(t('webhooks.panel.formErrorUrl'))
      return
    }
    if (events.length === 0) {
      setFormError(t('webhooks.panel.formErrorEvents'))
      return
    }
    setFormError(null)
    createHook.mutate(
      {
        url: url.trim(),
        events,
        project_id: projectId ?? null,
        description: description.trim() || null,
        format,
      },
      {
        onSuccess: (created) => {
          setSecret(created.secret)
          setUrl('')
          setDescription('')
          setEvents(['*'])
          setFormat('json')
        },
        onError: (err) => setFormError(errorText(err, t('webhooks.panel.formErrorCreate'))),
      },
    )
  }

  function rowAction(promise: Promise<unknown>): void {
    setRowError(null)
    promise.catch((err: unknown) => setRowError(errorText(err, t('webhooks.panel.formErrorAction'))))
  }

  return (
    <section aria-labelledby="webhooks-heading" className="mb-8">
      <h2 id="webhooks-heading" className="mb-1 text-lg font-semibold text-ink">
        {t('webhooks.panel.heading')}
      </h2>
      <p className="mb-3 text-sm text-muted">
        <Trans
          t={t}
          i18nKey={projectId ? 'webhooks.panel.descriptionProject' : 'webhooks.panel.descriptionOrg'}
          components={{ code: <code className="font-mono text-xs" /> }}
        />
      </p>

      {secret && <SecretReveal secret={secret} onDismiss={() => setSecret(null)} />}

      {hooksQuery.isLoading && (
        <div className="flex justify-center py-6">
          <Spinner label={t('webhooks.panel.loading')} />
        </div>
      )}
      {hooksQuery.isError && (
        <ErrorState
          title={t('webhooks.panel.loadError')}
          message={errorText(hooksQuery.error, t('webhooks.panel.loadErrorFallback'))}
          onRetry={() => void hooksQuery.refetch()}
        />
      )}
      {hooksQuery.data && hooks.length === 0 && (
        <p className="mb-3 text-sm text-muted">{t('webhooks.panel.empty')}</p>
      )}

      {hooks.length > 0 && (
        <ul className="mb-4 space-y-2">
          {hooks.map((hook) => (
            <li
              key={hook.id}
              className="rounded-md border border-line p-3 text-sm"
              data-testid={`webhook-row-${hook.id}`}
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <p className="truncate font-mono text-xs text-ink" title={hook.url}>
                    {hook.url}
                  </p>
                  <p className="text-xs text-muted">
                    {hook.format !== 'json' && `${t(`webhooks.panel.format.${hook.format}`)} · `}
                    {hook.description ? `${hook.description} · ` : ''}
                    {hook.events.includes('*')
                      ? t('webhooks.panel.allEvents')
                      : hook.events.join(', ')}
                    {!hook.is_active && ` · ${t('webhooks.panel.paused')}`}
                    {hook.last_response_status !== null &&
                      ` · ${t('webhooks.panel.lastResponse', { status: hook.last_response_status })}`}
                    {hook.project_id === null && projectId && ` · ${t('webhooks.panel.orgWide')}`}
                  </p>
                </div>
                {!readOnly && (
                  <div className="flex flex-wrap gap-1">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setOpenLog(openLog === hook.id ? null : hook.id)}
                    >
                      {openLog === hook.id
                        ? t('webhooks.panel.hideLog')
                        : t('webhooks.panel.deliveries')}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      disabled={testHook.isPending}
                      onClick={() =>
                        rowAction(
                          testHook.mutateAsync(hook.id).then(() => {
                            setTested(hook.id)
                            setOpenLog(hook.id)
                          }),
                        )
                      }
                    >
                      {t('webhooks.panel.sendTest')}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() =>
                        rowAction(
                          updateHook.mutateAsync({
                            id: hook.id,
                            body: { is_active: !hook.is_active },
                          }),
                        )
                      }
                    >
                      {hook.is_active ? t('webhooks.panel.pause') : t('webhooks.panel.resume')}
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() =>
                        rowAction(
                          updateHook
                            .mutateAsync({ id: hook.id, body: { rotate_secret: true } })
                            .then((updated) => setSecret(updated.secret)),
                        )
                      }
                    >
                      {t('webhooks.panel.rotateSecret')}
                    </Button>
                    {confirmDelete === hook.id ? (
                      <>
                        <Button
                          variant="danger"
                          size="sm"
                          onClick={() =>
                            rowAction(
                              deleteHook.mutateAsync(hook.id).then(() => setConfirmDelete(null)),
                            )
                          }
                        >
                          {t('webhooks.panel.confirmDelete')}
                        </Button>
                        <Button variant="ghost" size="sm" onClick={() => setConfirmDelete(null)}>
                          {t('common:cancel')}
                        </Button>
                      </>
                    ) : (
                      <Button variant="ghost" size="sm" onClick={() => setConfirmDelete(hook.id)}>
                        {t('common:delete')}
                      </Button>
                    )}
                  </div>
                )}
              </div>
              {tested === hook.id && (
                <p className="mt-1 text-xs text-muted" role="status">
                  {t('webhooks.panel.testQueued')}
                </p>
              )}
              {openLog === hook.id && (
                <div className="mt-2">
                  <DeliveryLog webhookId={hook.id} />
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {rowError && (
        <p className="mb-3 text-sm text-danger" role="alert">
          {rowError}
        </p>
      )}

      {!readOnly && (
        <form
          aria-label={t('webhooks.panel.newForm.ariaLabel')}
          className="rounded-lg border border-line p-3"
          onSubmit={(e) => {
            e.preventDefault()
            handleCreate()
          }}
        >
          <div className="flex flex-wrap items-end gap-3">
            <label className="flex min-w-[18rem] flex-1 flex-col gap-1 text-xs text-muted">
              {t('webhooks.panel.newForm.url')}
              <input
                type="url"
                aria-label={t('webhooks.panel.newForm.urlAriaLabel')}
                value={url}
                placeholder="https://example.com/hooks/annotations"
                onChange={(e) => setUrl(e.target.value)}
                className={inputClass}
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted">
              {t('webhooks.panel.newForm.description')}
              <input
                type="text"
                aria-label={t('webhooks.panel.newForm.descriptionAriaLabel')}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                className={inputClass}
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted">
              {t('webhooks.panel.newForm.format')}
              <select
                aria-label={t('webhooks.panel.newForm.formatAriaLabel')}
                value={format}
                onChange={(e) => setFormat(e.target.value as WebhookFormat)}
                className={inputClass}
              >
                <option value="json">{t('webhooks.panel.format.json')}</option>
                <option value="slack">{t('webhooks.panel.format.slack')}</option>
                <option value="teams">{t('webhooks.panel.format.teams')}</option>
              </select>
            </label>
          </div>
          <fieldset className="mt-3">
            <legend className="mb-1 text-xs text-muted">{t('webhooks.panel.newForm.eventsLegend')}</legend>
            <div className="flex flex-wrap gap-3 text-sm text-ink">
              <label className="flex items-center gap-1">
                <input
                  type="checkbox"
                  checked={events.includes('*')}
                  onChange={() => toggleEvent('*')}
                />
                {t('webhooks.panel.newForm.all')}
              </label>
              {WEBHOOK_EVENTS.map((name) => (
                <label key={name} className="flex items-center gap-1">
                  <input
                    type="checkbox"
                    checked={events.includes('*') || events.includes(name)}
                    disabled={events.includes('*')}
                    onChange={() => toggleEvent(name)}
                  />
                  {name}
                </label>
              ))}
            </div>
          </fieldset>
          <div className="mt-3 flex items-center gap-3">
            <Button type="submit" disabled={createHook.isPending}>
              {createHook.isPending ? t('webhooks.panel.newForm.creating') : t('webhooks.panel.newForm.submit')}
            </Button>
            {formError && (
              <span className="text-sm text-danger" role="alert">
                {formError}
              </span>
            )}
          </div>
        </form>
      )}
    </section>
  )
}
