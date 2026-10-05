import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import type { TFunction } from 'i18next'
import {
  useCheckConnector,
  useConnectors,
  useCreateConnector,
  useDeleteConnector,
  useMintConnectorEventToken,
  useRevokeConnectorEventToken,
  useUpdateConnector,
} from '@/api/queries'
import type { Connector, ConnectorCreate, ConnectorIdentity, ConnectorType } from '@/api/types'
import { ApiError } from '@/api/client'
import { Spinner } from '@/components/Spinner'
import { ErrorState } from '@/components/ErrorState'
import { EmptyState } from '@/components/EmptyState'
import { useBusinessFeature } from '@/components/BusinessBadge'
import { Button } from '@/components/Button'
import { useAuthStore } from '@/lib/store'
import i18n from '@/i18n'

const CONNECTOR_HINTS: Record<ConnectorType, string> = {
  azure_blob: '{"account_url": "https://<account>.blob.core.windows.net", "container": "…"}',
  local: '{"root": "/data"}',
  s3: '{"bucket": "…", "region": "eu-west-1"}',
  gcs: '{"bucket": "…"}',
  http: '{"base_url": "https://cdn.example.com/dataset/", "manifest": "manifest.txt"}',
  sharepoint: '{"drive_id": "b!…", "tenant_id": "…", "client_id": "…"}',
  databricks_volume:
    '{"host": "https://adb-….azuredatabricks.net", "volume_path": "/Volumes/<catalog>/<schema>/<volume>"}',
}

function connectorTypeOptions(
  t: TFunction<['admin', 'common']>,
): Array<{ value: ConnectorType; label: string; hint: string }> {
  return [
    { value: 'azure_blob', label: t('connectors.types.azureBlob'), hint: CONNECTOR_HINTS.azure_blob },
    { value: 'local', label: t('connectors.types.local'), hint: CONNECTOR_HINTS.local },
    { value: 's3', label: t('connectors.types.s3'), hint: CONNECTOR_HINTS.s3 },
    { value: 'gcs', label: t('connectors.types.gcs'), hint: CONNECTOR_HINTS.gcs },
    {
      value: 'http',
      label: t('connectors.types.http'),
      hint: `${CONNECTOR_HINTS.http} — ${t('connectors.httpReadOnlyNote')}`,
    },
    {
      value: 'sharepoint',
      label: t('connectors.types.sharepoint'),
      hint: `${CONNECTOR_HINTS.sharepoint} — ${t('connectors.sharepointNote')}`,
    },
    {
      value: 'databricks_volume',
      label: t('connectors.types.databricksVolume'),
      hint: `${CONNECTOR_HINTS.databricks_volume} — ${t('connectors.databricksVolumeNote')}`,
    },
  ]
}

const CONNECTOR_IDENTITY_OPTIONS: ConnectorIdentity[] = [
  'none',
  'managed_identity',
  'service_principal',
  'account_key',
  'sas_token',
  'iam_role',
  'access_key',
]

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? (error.detail ?? error.title) : i18n.t('admin:errors.unknown')
}

interface ConnectorFormValue {
  name: string
  type: ConnectorType
  identity_type: ConnectorIdentity
  secret_ref: string
  configText: string
}

const EMPTY_FORM: ConnectorFormValue = {
  name: '',
  type: 'azure_blob',
  identity_type: 'none',
  secret_ref: '',
  configText: '{}',
}

function connectorToForm(connector: Connector): ConnectorFormValue {
  return {
    name: connector.name,
    type: connector.type,
    identity_type: connector.identity_type,
    secret_ref: '',
    configText: JSON.stringify(connector.config, null, 2),
  }
}

interface ConnectorFormProps {
  title: string
  initial: ConnectorFormValue
  submitLabel: string
  pending: boolean
  errorText: string | null
  onSubmit: (value: ConnectorFormValue, config: Record<string, unknown>) => void
  onCancel?: () => void
}

function ConnectorForm({
  title,
  initial,
  submitLabel,
  pending,
  errorText,
  onSubmit,
  onCancel,
}: ConnectorFormProps): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const [value, setValue] = useState<ConnectorFormValue>(initial)
  const [configError, setConfigError] = useState<string | null>(null)

  const typeOptions = connectorTypeOptions(t)
  const sharepointLocked = useBusinessFeature('sharepoint') === false
  const selectedType = typeOptions.find((option) => option.value === value.type)

  /** Parses the config textarea; must be valid JSON and an object (not an array/primitive). */
  function parseConfig(
    text: string,
  ): { ok: true; value: Record<string, unknown> } | { ok: false; error: string } {
    try {
      const parsed = JSON.parse(text === '' ? '{}' : text)
      if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        return { ok: false, error: t('connectors.form.configErrorObject') }
      }
      return { ok: true, value: parsed as Record<string, unknown> }
    } catch {
      return { ok: false, error: t('connectors.form.configErrorJson') }
    }
  }

  function handleSubmit(event: FormEvent): void {
    event.preventDefault()
    const parsed = parseConfig(value.configText)
    if (!parsed.ok) {
      setConfigError(parsed.error)
      return
    }
    setConfigError(null)
    onSubmit(value, parsed.value)
  }

  return (
    <form
      onSubmit={handleSubmit}
      aria-label={title}
      className="mb-6 flex flex-col gap-3 rounded-lg border border-line bg-surface p-4"
    >
      <h2 className="text-sm font-semibold text-ink">{title}</h2>

      <div className="flex flex-col gap-1">
        <label htmlFor="connector-name" className="text-sm font-medium text-ink">
          {t('connectors.form.name')}
        </label>
        <input
          id="connector-name"
          type="text"
          required
          value={value.name}
          onChange={(event) => setValue((prev) => ({ ...prev, name: event.target.value }))}
          className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
            focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
            focus-visible:outline-accent"
        />
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="connector-type" className="text-sm font-medium text-ink">
          {t('connectors.form.type')}
        </label>
        <select
          id="connector-type"
          value={value.type}
          onChange={(event) =>
            setValue((prev) => ({ ...prev, type: event.target.value as ConnectorType }))
          }
          className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
            focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
            focus-visible:outline-accent"
        >
          {typeOptions.map((option) => (
            <option key={option.value} value={option.value}>
              {option.value === 'sharepoint' && sharepointLocked
                ? t('connectors.businessOption', { label: option.label })
                : option.label}
            </option>
          ))}
        </select>
        {selectedType && (
          <p className="text-xs text-muted">
            {t('connectors.form.configPrefix')} <code className="break-all font-mono">{selectedType.hint}</code>
          </p>
        )}
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="connector-identity" className="text-sm font-medium text-ink">
          {t('connectors.form.identity')}
        </label>
        <select
          id="connector-identity"
          value={value.identity_type}
          onChange={(event) =>
            setValue((prev) => ({
              ...prev,
              identity_type: event.target.value as ConnectorIdentity,
            }))
          }
          className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
            focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
            focus-visible:outline-accent"
        >
          {CONNECTOR_IDENTITY_OPTIONS.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="connector-secret-ref" className="text-sm font-medium text-ink">
          {t('connectors.form.secretRef')}
        </label>
        <input
          id="connector-secret-ref"
          type="text"
          placeholder="env://AZURE_STORAGE_KEY"
          value={value.secret_ref}
          onChange={(event) => setValue((prev) => ({ ...prev, secret_ref: event.target.value }))}
          className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
            focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
            focus-visible:outline-accent"
        />
        <p className="text-xs text-muted">{t('connectors.form.secretRefHint')}</p>
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="connector-config" className="text-sm font-medium text-ink">
          {t('connectors.form.configLabel')}
        </label>
        <textarea
          id="connector-config"
          rows={4}
          value={value.configText}
          onChange={(event) => setValue((prev) => ({ ...prev, configText: event.target.value }))}
          className="rounded-md border border-line bg-surface px-2 py-1.5 font-mono text-sm text-ink
            focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
            focus-visible:outline-accent"
        />
        {configError && <p className="text-sm text-danger">{configError}</p>}
      </div>

      {errorText && <p className="text-sm text-danger">{errorText}</p>}

      <div className="flex gap-2">
        <Button type="submit" variant="primary" disabled={pending}>
          {pending ? t('common:saving') : submitLabel}
        </Button>
        {onCancel && (
          <Button type="button" variant="ghost" onClick={onCancel}>
            {t('common:cancel')}
          </Button>
        )}
      </div>
    </form>
  )
}

/** The storage-event URL carries its token, so it is shown once and masked by default (SRC-3). */
function EventUrlReveal({ url, onDismiss }: { url: string; onDismiss: () => void }): JSX.Element {
  const { t } = useTranslation(['admin'])
  const [shown, setShown] = useState(false)
  return (
    <div
      role="status"
      className="mt-2 rounded-md border border-amber-400/60 bg-amber-500/10 p-3 text-sm text-ink"
    >
      <p className="mb-1 font-medium">{t('connectors.events.revealTitle')}</p>
      <code className="block break-all font-mono text-xs" data-testid="connector-event-url">
        {shown ? url : '•'.repeat(24)}
      </code>
      <div className="mt-2 flex gap-2">
        <Button variant="secondary" size="sm" onClick={() => setShown((v) => !v)}>
          {shown ? t('connectors.events.hide') : t('connectors.events.reveal')}
        </Button>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => {
            void navigator.clipboard?.writeText(url)
          }}
        >
          {t('connectors.events.copy')}
        </Button>
        <Button variant="ghost" size="sm" onClick={onDismiss}>
          {t('connectors.events.done')}
        </Button>
      </div>
    </div>
  )
}

interface ConnectorRowProps {
  connector: Connector
  onEdit: () => void
}

function ConnectorRow({ connector, onEdit }: ConnectorRowProps): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const checkConnector = useCheckConnector()
  const deleteConnector = useDeleteConnector()
  const mintEventToken = useMintConnectorEventToken()
  const revokeEventToken = useRevokeConnectorEventToken()
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [eventUrl, setEventUrl] = useState<string | null>(null)
  const eventsPending = mintEventToken.isPending || revokeEventToken.isPending

  function mintEventUrl(): void {
    mintEventToken.mutate(connector.id, {
      onSuccess: ({ path, token }) =>
        setEventUrl(`${window.location.origin}${path}?token=${encodeURIComponent(token)}`),
    })
  }

  return (
    <tr className="border-b border-line">
      <td className="px-3 py-2 text-sm text-ink">{connector.name}</td>
      <td className="px-3 py-2 text-sm text-ink">{connector.type}</td>
      <td className="px-3 py-2 text-sm text-ink">{connector.identity_type}</td>
      <td className="px-3 py-2 text-sm text-ink">
        {connector.has_secret ? t('connectors.row.secretSet') : t('connectors.row.secretNone')}
        {connector.events_enabled && (
          <span className="block text-xs text-muted">{t('connectors.events.on')}</span>
        )}
      </td>
      <td className="px-3 py-2 text-sm text-muted">
        {new Date(connector.created_at).toLocaleString()}
      </td>
      <td className="px-3 py-2 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="secondary"
            size="sm"
            disabled={checkConnector.isPending}
            onClick={() => checkConnector.mutate(connector.id)}
          >
            {checkConnector.isPending ? t('connectors.row.checking') : t('connectors.row.check')}
          </Button>
          <Button variant="secondary" size="sm" onClick={onEdit}>
            {t('common:edit')}
          </Button>
          <Button variant="secondary" size="sm" disabled={eventsPending} onClick={mintEventUrl}>
            {mintEventToken.isPending
              ? t('connectors.events.working')
              : connector.events_enabled
                ? t('connectors.events.rotate')
                : t('connectors.events.enable')}
          </Button>
          {connector.events_enabled && (
            <Button
              variant="ghost"
              size="sm"
              disabled={eventsPending}
              onClick={() =>
                revokeEventToken.mutate(connector.id, { onSuccess: () => setEventUrl(null) })
              }
            >
              {t('connectors.events.disable')}
            </Button>
          )}
          {!confirmingDelete && (
            <Button variant="danger" size="sm" onClick={() => setConfirmingDelete(true)}>
              {t('common:delete')}
            </Button>
          )}
          {confirmingDelete && (
            <>
              <span className="text-xs text-muted">{t('connectors.row.confirmDeleteQuestion')}</span>
              <Button
                variant="danger"
                size="sm"
                disabled={deleteConnector.isPending}
                onClick={() =>
                  deleteConnector.mutate(connector.id, {
                    onSettled: () => setConfirmingDelete(false),
                  })
                }
              >
                {deleteConnector.isPending
                  ? t('connectors.row.deleting')
                  : t('connectors.row.confirmDelete')}
              </Button>
              <Button variant="ghost" size="sm" onClick={() => setConfirmingDelete(false)}>
                {t('common:cancel')}
              </Button>
            </>
          )}
        </div>
        {checkConnector.isSuccess && checkConnector.variables === connector.id && (
          <p
            className={`mt-1 text-xs ${checkConnector.data.ok ? 'text-success' : 'text-danger'}`}
          >
            {checkConnector.data.ok ? t('connectors.row.checkOk') : t('connectors.row.checkFailed')}
            {checkConnector.data.messages.length > 0 && ` — ${checkConnector.data.messages.join('; ')}`}
          </p>
        )}
        {checkConnector.isError && checkConnector.variables === connector.id && (
          <p className="mt-1 text-xs text-danger">{errorMessage(checkConnector.error)}</p>
        )}
        {deleteConnector.isError && (
          <p className="mt-1 text-xs text-danger">{errorMessage(deleteConnector.error)}</p>
        )}
        {(mintEventToken.isError || revokeEventToken.isError) && (
          <p className="mt-1 text-xs text-danger">
            {errorMessage(mintEventToken.error ?? revokeEventToken.error)}
          </p>
        )}
        {eventUrl && <EventUrlReveal url={eventUrl} onDismiss={() => setEventUrl(null)} />}
      </td>
    </tr>
  )
}

/** Admin page for registering and managing storage connectors (create/update/delete/check are superuser-only). */
export function ConnectorsPage(): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const user = useAuthStore((state) => state.user)
  const isSuperuser = user?.is_superuser ?? false

  const connectorsQuery = useConnectors()
  const createConnector = useCreateConnector()
  const updateConnector = useUpdateConnector()

  const [editingId, setEditingId] = useState<string | null>(null)

  const connectors = connectorsQuery.data?.items ?? []
  const editingConnector = connectors.find((connector) => connector.id === editingId) ?? null

  function handleCreate(value: ConnectorFormValue, config: Record<string, unknown>): void {
    const payload: ConnectorCreate = {
      name: value.name,
      type: value.type,
      identity_type: value.identity_type,
      secret_ref: value.secret_ref === '' ? undefined : value.secret_ref,
      config,
    }
    createConnector.mutate(payload)
  }

  function handleUpdate(
    connector: Connector,
    initial: ConnectorFormValue,
    value: ConnectorFormValue,
    config: Record<string, unknown>,
  ): void {
    const payload: Record<string, unknown> = {}
    if (value.name !== initial.name) payload.name = value.name
    if (value.type !== initial.type) payload.type = value.type
    if (value.identity_type !== initial.identity_type) payload.identity_type = value.identity_type
    if (value.secret_ref !== '') payload.secret_ref = value.secret_ref
    if (JSON.stringify(config) !== JSON.stringify(connector.config)) payload.config = config

    updateConnector.mutate(
      { id: connector.id, payload },
      {
        onSuccess: () => setEditingId(null),
      },
    )
  }

  return (
    <div className="mx-auto w-full max-w-5xl flex-1 px-4 py-8 sm:px-6">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-xl font-semibold text-ink">{t('connectors.title')}</h1>
      </div>

      {!isSuperuser && <p className="mb-6 text-sm text-muted">{t('connectors.adminOnly')}</p>}

      {isSuperuser && !editingConnector && (
        <ConnectorForm
          key="create"
          title={t('connectors.form.createTitle')}
          initial={EMPTY_FORM}
          submitLabel={t('connectors.form.submitCreate')}
          pending={createConnector.isPending}
          errorText={createConnector.isError ? errorMessage(createConnector.error) : null}
          onSubmit={handleCreate}
        />
      )}

      {isSuperuser && editingConnector && (
        <ConnectorForm
          key={editingConnector.id}
          title={t('connectors.form.editTitle', { name: editingConnector.name })}
          initial={connectorToForm(editingConnector)}
          submitLabel={t('connectors.form.submitSave')}
          pending={updateConnector.isPending}
          errorText={updateConnector.isError ? errorMessage(updateConnector.error) : null}
          onSubmit={(value, config) =>
            handleUpdate(editingConnector, connectorToForm(editingConnector), value, config)
          }
          onCancel={() => setEditingId(null)}
        />
      )}

      {connectorsQuery.isLoading && (
        <div className="flex justify-center py-16">
          <Spinner label={t('connectors.loading')} />
        </div>
      )}

      {connectorsQuery.isError && (
        <ErrorState
          title={t('connectors.loadError')}
          message={errorMessage(connectorsQuery.error)}
          onRetry={() => void connectorsQuery.refetch()}
        />
      )}

      {!connectorsQuery.isLoading && !connectorsQuery.isError && connectors.length === 0 && (
        <EmptyState title={t('connectors.empty.title')} message={t('connectors.empty.message')} />
      )}

      {connectors.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-line">
          <table className="w-full text-left">
            <thead>
              <tr className="border-b border-line bg-surface">
                <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
                  {t('connectors.table.name')}
                </th>
                <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
                  {t('connectors.table.type')}
                </th>
                <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
                  {t('connectors.table.identity')}
                </th>
                <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
                  {t('connectors.table.secret')}
                </th>
                <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
                  {t('connectors.table.created')}
                </th>
                {isSuperuser && (
                  <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
                    {t('connectors.table.actions')}
                  </th>
                )}
              </tr>
            </thead>
            <tbody>
              {connectors.map((connector) =>
                isSuperuser ? (
                  <ConnectorRow
                    key={connector.id}
                    connector={connector}
                    onEdit={() => setEditingId(connector.id)}
                  />
                ) : (
                  <tr key={connector.id} className="border-b border-line">
                    <td className="px-3 py-2 text-sm text-ink">{connector.name}</td>
                    <td className="px-3 py-2 text-sm text-ink">{connector.type}</td>
                    <td className="px-3 py-2 text-sm text-ink">{connector.identity_type}</td>
                    <td className="px-3 py-2 text-sm text-ink">
                      {connector.has_secret
                        ? t('connectors.row.secretSet')
                        : t('connectors.row.secretNone')}
                    </td>
                    <td className="px-3 py-2 text-sm text-muted">
                      {new Date(connector.created_at).toLocaleString()}
                    </td>
                  </tr>
                ),
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
