import { useState } from 'react'
import type { FormEvent } from 'react'
import { Trans, useTranslation } from 'react-i18next'
import type { TFunction } from 'i18next'
import {
  useCheckModel,
  useCreateModel,
  useCorrectionMetrics,
  useCreateModelVersion,
  useDeleteModel,
  useModelVersions,
  useModels,
  useUpdateModel,
} from '@/api/queries'
import type {
  Model,
  ModelCreate,
  ModelIdentity,
  ModelIdentityConfig,
  ModelTask,
  ModelUpdate,
  ModelVersion,
  ModelVersionCreate,
} from '@/api/types'
import { ApiError } from '@/api/client'
import { Spinner } from '@/components/Spinner'
import { ErrorState } from '@/components/ErrorState'
import { EmptyState } from '@/components/EmptyState'
import { Button } from '@/components/Button'
import { ImportVersionForm, MlPlatformsPanel } from '@/components/MlPlatformsPanel'
import { ModelFamilyPanel } from '@/features/model-family/ModelFamilyPanel'
import { useAuthStore } from '@/lib/store'
import i18n from '@/i18n'
import { safeHref } from '../lib/url'

function modelTaskOptions(
  t: TFunction<['admin', 'common']>,
): Array<{ value: ModelTask; label: string }> {
  return [
    { value: 'detect', label: t('models.tasks.detect') },
    { value: 'segment', label: t('models.tasks.segment') },
    { value: 'classify', label: t('models.tasks.classify') },
    { value: 'ner', label: t('models.tasks.ner') },
    { value: 'llm', label: t('models.tasks.llm') },
    { value: 'ocr', label: t('models.tasks.ocr') },
  ]
}

const MODEL_IDENTITY_OPTIONS: ModelIdentity[] = [
  'none',
  'api_key',
  'bearer',
  'service_principal',
  'managed_identity',
]

/** Identities that sign in to Microsoft Entra for a token (BYOM-3). */
function isEntra(identity: ModelIdentity): boolean {
  return identity === 'service_principal' || identity === 'managed_identity'
}

/** Identities whose credential is behind `secret_ref`. */
function needsSecret(identity: ModelIdentity): boolean {
  return identity !== 'none' && identity !== 'managed_identity'
}

const INPUT_CLASS =
  'rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink ' +
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent'

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? (error.detail ?? error.title) : i18n.t('admin:errors.unknown')
}

interface ModelFormValue {
  name: string
  task: ModelTask
  endpoint_url: string
  identity_type: ModelIdentity
  secret_ref: string
  scope: string
  tenant_id: string
  client_id: string
}

const EMPTY_MODEL_FORM: ModelFormValue = {
  name: '',
  task: 'detect',
  endpoint_url: '',
  identity_type: 'none',
  secret_ref: '',
  scope: '',
  tenant_id: '',
  client_id: '',
}

function sameConfig(a: ModelIdentityConfig | null, b: ModelIdentityConfig | null): boolean {
  return JSON.stringify(a) === JSON.stringify(b)
}

/** `identity_config` for the form's identity: only the fields it uses. */
function formIdentityConfig(value: ModelFormValue): ModelIdentityConfig | null {
  if (value.identity_type === 'managed_identity') {
    return { scope: value.scope.trim(), client_id: value.client_id.trim() || null }
  }
  if (value.identity_type === 'service_principal') {
    return {
      scope: value.scope.trim(),
      tenant_id: value.tenant_id.trim(),
      client_id: value.client_id.trim(),
    }
  }
  return null
}

function modelToForm(model: Model): ModelFormValue {
  return {
    name: model.name,
    task: model.task,
    endpoint_url: model.endpoint_url ?? '',
    identity_type: model.identity_type,
    secret_ref: '',
    scope: model.identity_config?.scope ?? '',
    tenant_id: model.identity_config?.tenant_id ?? '',
    client_id: model.identity_config?.client_id ?? '',
  }
}

interface ModelFormProps {
  title: string
  initial: ModelFormValue
  submitLabel: string
  pending: boolean
  errorText: string | null
  onSubmit: (value: ModelFormValue) => void
  onCancel?: () => void
}

/** Register/edit form for a model (ML-1, BYOM-2): superuser only. */
function ModelForm({
  title,
  initial,
  submitLabel,
  pending,
  errorText,
  onSubmit,
  onCancel,
}: ModelFormProps): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const [value, setValue] = useState<ModelFormValue>(initial)
  const [validationError, setValidationError] = useState<string | null>(null)
  const taskOptions = modelTaskOptions(t)

  function handleSubmit(event: FormEvent): void {
    event.preventDefault()

    // Empty is allowed: an external producer such as an AI agent (API-8).
    if (value.endpoint_url.trim() !== '' && !/^https?:\/\//i.test(value.endpoint_url)) {
      setValidationError(t('models.form.errorEndpoint'))
      return
    }
    if (needsSecret(value.identity_type) && value.secret_ref.trim() === '') {
      setValidationError(t('models.form.errorSecretRequired'))
      return
    }
    if (isEntra(value.identity_type) && !value.scope.trim().endsWith('/.default')) {
      setValidationError(t('models.form.errorScope'))
      return
    }
    if (
      value.identity_type === 'service_principal' &&
      (value.tenant_id.trim() === '' || value.client_id.trim() === '')
    ) {
      setValidationError(t('models.form.errorServicePrincipal'))
      return
    }
    setValidationError(null)
    onSubmit(value)
  }

  return (
    <form
      onSubmit={handleSubmit}
      aria-label={title}
      className="mb-6 flex flex-col gap-3 rounded-lg border border-line bg-surface p-4"
    >
      <h2 className="text-sm font-semibold text-ink">{title}</h2>

      <div className="flex flex-col gap-1">
        <label htmlFor="model-name" className="text-sm font-medium text-ink">
          {t('models.form.name')}
        </label>
        <input
          id="model-name"
          type="text"
          required
          value={value.name}
          onChange={(event) => setValue((prev) => ({ ...prev, name: event.target.value }))}
          className={INPUT_CLASS}
        />
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="model-task" className="text-sm font-medium text-ink">
          {t('models.form.task')}
        </label>
        <select
          id="model-task"
          value={value.task}
          onChange={(event) =>
            setValue((prev) => ({ ...prev, task: event.target.value as ModelTask }))
          }
          className={INPUT_CLASS}
        >
          {taskOptions.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="model-endpoint" className="text-sm font-medium text-ink">
          {t('models.form.endpoint')}
        </label>
        <input
          id="model-endpoint"
          type="text"
          aria-describedby="model-endpoint-hint"
          value={value.endpoint_url}
          onChange={(event) => setValue((prev) => ({ ...prev, endpoint_url: event.target.value }))}
          className={INPUT_CLASS}
        />
        <p id="model-endpoint-hint" className="text-xs text-muted">
          {t('models.form.endpointHint')}
        </p>
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="model-identity" className="text-sm font-medium text-ink">
          {t('models.form.identity')}
        </label>
        <select
          id="model-identity"
          value={value.identity_type}
          onChange={(event) =>
            setValue((prev) => ({
              ...prev,
              identity_type: event.target.value as ModelIdentity,
            }))
          }
          className={INPUT_CLASS}
        >
          {MODEL_IDENTITY_OPTIONS.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
      </div>

      {isEntra(value.identity_type) && (
        <div className="flex flex-col gap-1">
          <label htmlFor="model-scope" className="text-sm font-medium text-ink">
            {t('models.form.scope')}
          </label>
          <input
            id="model-scope"
            type="text"
            placeholder="https://ml.azure.com/.default"
            aria-describedby="model-scope-hint"
            value={value.scope}
            onChange={(event) => setValue((prev) => ({ ...prev, scope: event.target.value }))}
            className={INPUT_CLASS}
          />
          <p id="model-scope-hint" className="text-xs text-muted">
            {t('models.form.scopeHint')}
          </p>
        </div>
      )}

      {value.identity_type === 'service_principal' && (
        <div className="flex flex-col gap-1">
          <label htmlFor="model-tenant-id" className="text-sm font-medium text-ink">
            {t('models.form.tenantId')}
          </label>
          <input
            id="model-tenant-id"
            type="text"
            value={value.tenant_id}
            onChange={(event) => setValue((prev) => ({ ...prev, tenant_id: event.target.value }))}
            className={INPUT_CLASS}
          />
        </div>
      )}

      {isEntra(value.identity_type) && (
        <div className="flex flex-col gap-1">
          <label htmlFor="model-client-id" className="text-sm font-medium text-ink">
            {value.identity_type === 'managed_identity'
              ? t('models.form.managedIdentityClientId')
              : t('models.form.clientId')}
          </label>
          <input
            id="model-client-id"
            type="text"
            aria-describedby={
              value.identity_type === 'managed_identity' ? 'model-client-id-hint' : undefined
            }
            value={value.client_id}
            onChange={(event) => setValue((prev) => ({ ...prev, client_id: event.target.value }))}
            className={INPUT_CLASS}
          />
          {value.identity_type === 'managed_identity' && (
            <p id="model-client-id-hint" className="text-xs text-muted">
              {t('models.form.managedIdentityClientIdHint')}
            </p>
          )}
        </div>
      )}

      {needsSecret(value.identity_type) && (
        <div className="flex flex-col gap-1">
          <label htmlFor="model-secret-ref" className="text-sm font-medium text-ink">
            {value.identity_type === 'service_principal'
              ? t('models.form.clientSecretRef')
              : t('models.form.secretRef')}
          </label>
          <input
            id="model-secret-ref"
            type="text"
            placeholder="env://MODEL_API_KEY"
            value={value.secret_ref}
            onChange={(event) => setValue((prev) => ({ ...prev, secret_ref: event.target.value }))}
            className={INPUT_CLASS}
          />
          <p className="text-xs text-muted">{t('models.form.secretRefHint')}</p>
        </div>
      )}

      {validationError && <p className="text-sm text-danger">{validationError}</p>}
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

interface MappingRow {
  modelClass: string
  platformClass: string
  drop: boolean
}

const EMPTY_MAPPING_ROW: MappingRow = { modelClass: '', platformClass: '', drop: false }

interface AddVersionFormProps {
  pending: boolean
  errorText: string | null
  onSubmit: (payload: ModelVersionCreate) => void
  onCancel: () => void
}

/** Parse an optional JSON-object field; returns the error text when it is not one. */
function parseJsonObject(
  t: TFunction<['admin', 'common']>,
  text: string,
  label: string,
): Record<string, unknown> | undefined | string {
  if (text.trim() === '') return undefined
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    return t('models.addVersion.errorInvalidJson', { label })
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return t('models.addVersion.errorNotObject', { label })
  }
  return parsed as Record<string, unknown>
}

/** Add-version form (BYOM-3): version number, class-mapping editor, optional metrics JSON. */
function AddVersionForm({
  pending,
  errorText,
  onSubmit,
  onCancel,
}: AddVersionFormProps): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const [version, setVersion] = useState('')
  const [rows, setRows] = useState<MappingRow[]>([{ ...EMPTY_MAPPING_ROW }])
  const [metricsText, setMetricsText] = useState('')
  const [snapshotId, setSnapshotId] = useState('')
  const [snapshotDigest, setSnapshotDigest] = useState('')
  const [runText, setRunText] = useState('')
  const [validationError, setValidationError] = useState<string | null>(null)

  function updateRow(index: number, patch: Partial<MappingRow>): void {
    setRows((prev) => prev.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  }

  function addRow(): void {
    setRows((prev) => [...prev, { ...EMPTY_MAPPING_ROW }])
  }

  function removeRow(index: number): void {
    setRows((prev) => prev.filter((_, i) => i !== index))
  }

  function handleSubmit(event: FormEvent): void {
    event.preventDefault()

    const classMapping: Record<string, string | null> = {}
    for (const row of rows) {
      const key = row.modelClass.trim()
      if (!key) continue
      classMapping[key] = row.drop ? null : row.platformClass.trim()
    }

    const metrics = parseJsonObject(t, metricsText, t('models.addVersion.labelMetrics'))
    if (typeof metrics === 'string') {
      setValidationError(metrics)
      return
    }
    const trainingRun = parseJsonObject(t, runText, t('models.addVersion.labelTrainingRun'))
    if (typeof trainingRun === 'string') {
      setValidationError(trainingRun)
      return
    }
    const digest = snapshotDigest.trim().toLowerCase()
    if (digest !== '' && !/^[0-9a-f]{64}$/.test(digest)) {
      setValidationError(t('models.addVersion.errorDigest'))
      return
    }

    let versionNumber: number | undefined
    if (version.trim() !== '') {
      const parsedVersion = Number(version)
      if (!Number.isInteger(parsedVersion) || parsedVersion < 1) {
        setValidationError(t('models.addVersion.errorVersionPositive'))
        return
      }
      versionNumber = parsedVersion
    }

    setValidationError(null)

    const payload: ModelVersionCreate = {}
    if (versionNumber !== undefined) payload.version = versionNumber
    if (Object.keys(classMapping).length > 0) payload.class_mapping = classMapping
    if (metrics) payload.metrics = metrics
    if (snapshotId.trim() !== '') payload.snapshot_id = snapshotId.trim()
    if (digest !== '') payload.snapshot_digest = digest
    if (trainingRun) payload.training_run = trainingRun

    onSubmit(payload)
  }

  return (
    <form
      onSubmit={handleSubmit}
      aria-label={t('models.addVersion.ariaLabel')}
      className="mt-3 flex flex-col gap-3 rounded-md border border-line p-3"
    >
      <div className="flex flex-col gap-1">
        <label htmlFor="version-number" className="text-sm font-medium text-ink">
          {t('models.addVersion.versionNumber')}
        </label>
        <input
          id="version-number"
          type="text"
          placeholder={t('models.addVersion.next')}
          value={version}
          onChange={(event) => setVersion(event.target.value)}
          className={INPUT_CLASS}
        />
      </div>

      <div className="flex flex-col gap-2">
        <p className="text-sm font-medium text-ink">{t('models.addVersion.classMapping')}</p>
        {rows.map((row, index) => (
          <div key={index} className="flex flex-wrap items-center gap-2">
            <input
              aria-label={t('models.addVersion.modelClass')}
              type="text"
              value={row.modelClass}
              onChange={(event) => updateRow(index, { modelClass: event.target.value })}
              className={INPUT_CLASS}
            />
            <span className="text-sm text-muted">→</span>
            <input
              aria-label={t('models.addVersion.platformClass')}
              type="text"
              value={row.platformClass}
              disabled={row.drop}
              onChange={(event) => updateRow(index, { platformClass: event.target.value })}
              className={INPUT_CLASS}
            />
            <label className="flex items-center gap-1 text-xs text-muted">
              <input
                type="checkbox"
                aria-label={t('models.addVersion.drop')}
                checked={row.drop}
                onChange={(event) => updateRow(index, { drop: event.target.checked })}
              />
              {t('models.addVersion.drop')}
            </label>
            <Button type="button" variant="ghost" size="sm" onClick={() => removeRow(index)}>
              {t('common:remove')}
            </Button>
          </div>
        ))}
        <Button type="button" variant="ghost" size="sm" className="self-start" onClick={addRow}>
          {t('models.addVersion.addRow')}
        </Button>
      </div>

      <div className="flex flex-col gap-1">
        <label htmlFor="version-metrics" className="text-sm font-medium text-ink">
          {t('models.addVersion.metrics')}
        </label>
        <textarea
          id="version-metrics"
          rows={3}
          value={metricsText}
          onChange={(event) => setMetricsText(event.target.value)}
          className={`${INPUT_CLASS} font-mono`}
        />
      </div>

      <fieldset className="flex flex-col gap-2 rounded-md border border-line p-2">
        <legend className="px-1 text-sm font-medium text-ink">
          {t('models.addVersion.lineageLegend')}
        </legend>
        <p className="text-xs text-muted">
          <Trans
            t={t}
            i18nKey="models.addVersion.lineageHint"
            components={{ code: <code className="font-mono" /> }}
          />
        </p>
        <div className="flex flex-col gap-1">
          <label htmlFor="version-snapshot-id" className="text-sm text-ink">
            {t('models.addVersion.snapshotId')}
          </label>
          <input
            id="version-snapshot-id"
            value={snapshotId}
            onChange={(event) => setSnapshotId(event.target.value)}
            className={`${INPUT_CLASS} font-mono`}
            placeholder={t('models.addVersion.optional')}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="version-snapshot-digest" className="text-sm text-ink">
            {t('models.addVersion.snapshotDigest')}
          </label>
          <input
            id="version-snapshot-digest"
            value={snapshotDigest}
            onChange={(event) => setSnapshotDigest(event.target.value)}
            className={`${INPUT_CLASS} font-mono`}
            placeholder={t('models.addVersion.optional')}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="version-training-run" className="text-sm text-ink">
            {t('models.addVersion.trainingRun')}
          </label>
          <textarea
            id="version-training-run"
            rows={2}
            value={runText}
            onChange={(event) => setRunText(event.target.value)}
            className={`${INPUT_CLASS} font-mono`}
            placeholder='{"id": "run-42", "url": "https://…"}'
          />
        </div>
      </fieldset>

      {validationError && <p className="text-sm text-danger">{validationError}</p>}
      {errorText && <p className="text-sm text-danger">{errorText}</p>}

      <div className="flex gap-2">
        <Button type="submit" variant="primary" disabled={pending}>
          {pending ? t('models.addVersion.adding') : t('models.addVersion.submit')}
        </Button>
        <Button type="button" variant="ghost" onClick={onCancel}>
          {t('common:cancel')}
        </Button>
      </div>
    </form>
  )
}

function pct(value: number | null): string {
  return value === null ? '—' : `${Math.round(value * 100)} %`
}

/** One line describing what a version was trained on (EXP-8), or nothing. */
export function describeLineage(version: ModelVersion): string | null {
  if (!version.snapshot_id && !version.snapshot_digest) return null
  const parts: string[] = []
  if (version.snapshot_id) {
    parts.push(
      i18n.t('admin:models.versionsPanel.snapshotPart', { id: version.snapshot_id.slice(0, 8) }),
    )
  }
  if (version.snapshot_digest) {
    parts.push(
      i18n.t('admin:models.versionsPanel.digestPart', {
        digest: version.snapshot_digest.slice(0, 12),
      }),
    )
  }
  const run = version.training_run
  if (run) {
    const id = typeof run.id === 'string' || typeof run.id === 'number' ? String(run.id) : null
    if (id) parts.push(i18n.t('admin:models.versionsPanel.runPart', { id }))
  }
  return i18n.t('admin:models.versionsPanel.trainedOn', { parts: parts.join(' · ') })
}

/** "Quantized from v1" for a derived version (EXP-8), or nothing. */
export function describeDerivation(
  version: ModelVersion,
  siblings: ModelVersion[],
): string | null {
  if (!version.parent_version_id || !version.derivation) return null
  const parent = siblings.find((v) => v.id === version.parent_version_id)
  return i18n.t(`admin:models.versionsPanel.derivedFrom.${version.derivation}`, {
    parent: parent ? `v${parent.version}` : i18n.t('admin:models.versionsPanel.otherModelParent'),
  })
}

interface VersionMetricsProps {
  modelId: string
  versionId: string
}

/** Correction metrics for one version (ML-5), fetched on demand. */
function VersionMetrics({ modelId, versionId }: VersionMetricsProps): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const [open, setOpen] = useState(false)
  const metricsQuery = useCorrectionMetrics(modelId, versionId, undefined, open)
  const metrics = metricsQuery.data

  return (
    <div className="mt-2">
      <button
        type="button"
        className="text-xs text-accent underline-offset-2 hover:underline"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        {open ? t('models.metrics.hide') : t('models.metrics.show')}
      </button>
      {open && metricsQuery.isLoading && (
        <div className="mt-1">
          <Spinner label={t('models.metrics.loading')} />
        </div>
      )}
      {open && metricsQuery.isError && (
        <p className="mt-1 text-xs text-danger" role="alert">
          {errorMessage(metricsQuery.error)}
        </p>
      )}
      {open && metrics && (
        <div
          className="mt-1 text-xs text-muted"
          role="region"
          aria-label={t('models.metrics.regionLabel', { versionId })}
        >
          <p>
            {t('models.metrics.summary', {
              predicted: metrics.items_predicted,
              corrected: metrics.items_corrected,
              acceptedUnchanged: metrics.items_accepted_unchanged,
              pending: metrics.items_pending,
            })}
          </p>
          {metrics.items_corrected > 0 && (
            <>
              <p className="mt-0.5 text-ink">
                {t('models.metrics.scores', {
                  precision: pct(metrics.precision),
                  recall: pct(metrics.recall),
                })}
                {metrics.mean_iou_adjusted !== null &&
                  t('models.metrics.meanIou', { value: metrics.mean_iou_adjusted.toFixed(2) })}
              </p>
              <p className="mt-0.5">
                {t('models.metrics.shapes', {
                  kept: metrics.shapes.kept,
                  adjusted: metrics.shapes.adjusted,
                  relabeled: metrics.shapes.relabeled,
                  deleted: metrics.shapes.deleted,
                  added: metrics.shapes.added,
                })}
              </p>
              {metrics.classes.length > 0 && (
                <table className="mt-1">
                  <thead>
                    <tr>
                      <th className="pr-3 text-left font-medium">
                        {t('models.metrics.columnClass')}
                      </th>
                      <th className="pr-3 text-right font-medium">
                        {t('models.metrics.columnModel')}
                      </th>
                      <th className="pr-3 text-right font-medium">
                        {t('models.metrics.columnPrecision')}
                      </th>
                      <th className="text-right font-medium">{t('models.metrics.columnRecall')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {metrics.classes.map((row) => (
                      <tr key={row.name}>
                        <td className="pr-3 text-ink">{row.name}</td>
                        <td className="pr-3 text-right">{row.model}</td>
                        <td className="pr-3 text-right">{pct(row.precision)}</td>
                        <td className="text-right">{pct(row.recall)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

interface VersionsPanelProps {
  modelId: string
  isSuperuser: boolean
}

/** Lists a model's versions (oldest first) and, for superusers, lets one be added. */
function VersionsPanel({ modelId, isSuperuser }: VersionsPanelProps): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const versionsQuery = useModelVersions(modelId)
  const createVersion = useCreateModelVersion(modelId)
  const [showForm, setShowForm] = useState(false)
  const [importing, setImporting] = useState(false)
  const [showFamily, setShowFamily] = useState(false)

  const versions = versionsQuery.data?.items ?? []

  return (
    <div>
      <h3 className="mb-2 text-sm font-semibold text-ink">{t('models.versionsPanel.heading')}</h3>

      {versionsQuery.isLoading && (
        <div className="flex justify-center py-4">
          <Spinner label={t('models.versionsPanel.loading')} />
        </div>
      )}

      {versionsQuery.isError && (
        <ErrorState
          title={t('models.versionsPanel.loadError')}
          message={errorMessage(versionsQuery.error)}
          onRetry={() => void versionsQuery.refetch()}
        />
      )}

      {!versionsQuery.isLoading && !versionsQuery.isError && versions.length === 0 && (
        <p className="text-sm text-muted">{t('models.versionsPanel.empty')}</p>
      )}

      {versions.length > 0 && (
        <ul className="mb-3 space-y-2">
          {versions.map((version) => (
            <li key={version.id} className="rounded-md border border-line p-2 text-sm">
              <p className="text-ink">
                v{version.version} · {new Date(version.created_at).toLocaleString()}
              </p>
              {describeDerivation(version, versions) && (
                <p className="mt-0.5 text-xs text-muted">
                  {describeDerivation(version, versions)}
                </p>
              )}
              {describeLineage(version) && (
                <p className="mt-0.5 text-xs text-muted" title={version.snapshot_digest ?? ''}>
                  {safeHref(version.training_run?.url) ? (
                    <a
                      href={safeHref(version.training_run?.url)}
                      target="_blank"
                      rel="noreferrer"
                      className="underline"
                    >
                      {describeLineage(version)}
                    </a>
                  ) : (
                    describeLineage(version)
                  )}
                </p>
              )}
              {Object.keys(version.class_mapping).length > 0 && (
                <ul className="mt-1 space-y-0.5 text-xs text-muted">
                  {Object.entries(version.class_mapping).map(([from, to]) => (
                    <li key={from}>
                      {from} → {to ?? t('models.versionsPanel.dropped')}
                    </li>
                  ))}
                </ul>
              )}
              <VersionMetrics modelId={modelId} versionId={version.id} />
            </li>
          ))}
        </ul>
      )}

      {versions.length > 0 && (
        <div className="mb-3">
          <button
            type="button"
            className="text-xs text-accent underline-offset-2 hover:underline"
            aria-expanded={showFamily}
            onClick={() => setShowFamily((v) => !v)}
          >
            {showFamily
              ? t('models.versionsPanel.hideFamily')
              : t('models.versionsPanel.showFamily')}
          </button>
          {showFamily && (
            <div className="mt-2 rounded-md border border-line p-3">
              <ModelFamilyPanel modelId={modelId} />
            </div>
          )}
        </div>
      )}

      {isSuperuser && !showForm && !importing && (
        <div className="flex gap-2">
          <Button variant="secondary" size="sm" onClick={() => setShowForm(true)}>
            {t('models.versionsPanel.addVersion')}
          </Button>
          <Button variant="secondary" size="sm" onClick={() => setImporting(true)}>
            {t('models.versionsPanel.importVersion')}
          </Button>
        </div>
      )}

      {isSuperuser && importing && (
        <ImportVersionForm modelId={modelId} onDone={() => setImporting(false)} />
      )}

      {isSuperuser && showForm && (
        <AddVersionForm
          pending={createVersion.isPending}
          errorText={createVersion.isError ? errorMessage(createVersion.error) : null}
          onSubmit={(payload) =>
            createVersion.mutate(payload, { onSuccess: () => setShowForm(false) })
          }
          onCancel={() => setShowForm(false)}
        />
      )}
    </div>
  )
}

interface ModelRowProps {
  model: Model
  isSuperuser: boolean
  expanded: boolean
  onToggleVersions: () => void
  onEdit: () => void
}

function ModelRow({
  model,
  isSuperuser,
  expanded,
  onToggleVersions,
  onEdit,
}: ModelRowProps): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const checkModel = useCheckModel()
  const deleteModel = useDeleteModel()
  const [confirmingDelete, setConfirmingDelete] = useState(false)

  return (
    <>
      <tr className="border-b border-line">
        <td className="px-3 py-2 text-sm text-ink">{model.name}</td>
        <td className="px-3 py-2 text-sm text-ink">{model.task}</td>
        <td className="px-3 py-2 text-sm text-ink">
          {model.endpoint_url ?? t('models.row.external')}
        </td>
        <td className="px-3 py-2 text-sm text-ink">{model.identity_type}</td>
        <td className="px-3 py-2 text-sm text-ink">
          {model.has_secret ? t('models.row.secretSet') : t('models.row.secretNone')}
        </td>
        <td className="px-3 py-2 text-sm text-muted">
          {new Date(model.created_at).toLocaleString()}
        </td>
        <td className="px-3 py-2 text-sm">
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="secondary" size="sm" onClick={onToggleVersions}>
              {expanded ? t('models.row.hideVersions') : t('models.row.versions')}
            </Button>
            {isSuperuser && (
              <>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={checkModel.isPending}
                  onClick={() => checkModel.mutate(model.id)}
                >
                  {checkModel.isPending ? t('models.row.checking') : t('models.row.check')}
                </Button>
                <Button variant="secondary" size="sm" onClick={onEdit}>
                  {t('common:edit')}
                </Button>
                {!confirmingDelete && (
                  <Button variant="danger" size="sm" onClick={() => setConfirmingDelete(true)}>
                    {t('common:delete')}
                  </Button>
                )}
                {confirmingDelete && (
                  <>
                    <span className="text-xs text-muted">
                      {t('models.row.confirmDeleteQuestion')}
                    </span>
                    <Button
                      variant="danger"
                      size="sm"
                      disabled={deleteModel.isPending}
                      onClick={() =>
                        deleteModel.mutate(model.id, {
                          onSettled: () => setConfirmingDelete(false),
                        })
                      }
                    >
                      {deleteModel.isPending
                        ? t('models.row.deleting')
                        : t('models.row.confirmDelete')}
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => setConfirmingDelete(false)}>
                      {t('common:cancel')}
                    </Button>
                  </>
                )}
              </>
            )}
          </div>
          {checkModel.isSuccess && checkModel.variables === model.id && (
            <p className={`mt-1 text-xs ${checkModel.data.ok ? 'text-success' : 'text-danger'}`}>
              {checkModel.data.ok ? t('models.row.checkOk') : t('models.row.checkFailed')}
              {checkModel.data.messages.length > 0 && ` — ${checkModel.data.messages.join('; ')}`}
            </p>
          )}
          {checkModel.isError && checkModel.variables === model.id && (
            <p className="mt-1 text-xs text-danger">{errorMessage(checkModel.error)}</p>
          )}
          {deleteModel.isError && (
            <p className="mt-1 text-xs text-danger">{errorMessage(deleteModel.error)}</p>
          )}
        </td>
      </tr>
      {expanded && (
        <tr className="border-b border-line">
          <td colSpan={7} className="bg-surface/50 px-3 py-3">
            <VersionsPanel modelId={model.id} isSuperuser={isSuperuser} />
          </td>
        </tr>
      )}
    </>
  )
}

/** Admin page for the model registry (ML-1, BYOM-2, BYOM-3): register/update/delete/check
 * are superuser-only; any org member may list models and versions. */
export function ModelsPage(): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const user = useAuthStore((state) => state.user)
  const isSuperuser = user?.is_superuser ?? false

  const modelsQuery = useModels()
  const createModel = useCreateModel()
  const updateModel = useUpdateModel()

  const [editingId, setEditingId] = useState<string | null>(null)
  const [expandedId, setExpandedId] = useState<string | null>(null)

  const models = modelsQuery.data?.items ?? []
  const editingModel = models.find((model) => model.id === editingId) ?? null

  function handleCreate(value: ModelFormValue): void {
    const payload: ModelCreate = {
      name: value.name,
      task: value.task,
      endpoint_url: value.endpoint_url.trim() || null,
      identity_type: value.identity_type,
      secret_ref: needsSecret(value.identity_type) ? value.secret_ref : undefined,
    }
    const identityConfig = formIdentityConfig(value)
    if (identityConfig) payload.identity_config = identityConfig
    createModel.mutate(payload)
  }

  function handleUpdate(model: Model, initial: ModelFormValue, value: ModelFormValue): void {
    const payload: ModelUpdate = {}
    if (value.name !== initial.name) payload.name = value.name
    if (value.task !== initial.task) payload.task = value.task
    // An endpoint can be set or changed here, not removed.
    if (value.endpoint_url.trim() !== '' && value.endpoint_url !== initial.endpoint_url) {
      payload.endpoint_url = value.endpoint_url
    }
    if (value.identity_type !== initial.identity_type) payload.identity_type = value.identity_type
    if (!needsSecret(value.identity_type)) {
      // A managed identity takes no secret; drop the one the old identity had.
      if (model.has_secret) payload.secret_ref = null
    } else if (value.secret_ref !== '') {
      payload.secret_ref = value.secret_ref
    }
    const identityConfig = formIdentityConfig(value)
    if (!sameConfig(identityConfig, formIdentityConfig(initial))) {
      // Replaced whole on the server; null clears it when leaving Entra.
      payload.identity_config = identityConfig
    }

    updateModel.mutate({ id: model.id, payload }, { onSuccess: () => setEditingId(null) })
  }

  return (
    <div className="mx-auto w-full max-w-5xl flex-1 px-4 py-8 sm:px-6">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-xl font-semibold text-ink">{t('models.title')}</h1>
      </div>

      {!isSuperuser && <p className="mb-6 text-sm text-muted">{t('models.adminOnly')}</p>}

      {isSuperuser && !editingModel && (
        <ModelForm
          key="create"
          title={t('models.form.createTitle')}
          initial={EMPTY_MODEL_FORM}
          submitLabel={t('models.form.submitCreate')}
          pending={createModel.isPending}
          errorText={createModel.isError ? errorMessage(createModel.error) : null}
          onSubmit={handleCreate}
        />
      )}

      {isSuperuser && editingModel && (
        <ModelForm
          key={editingModel.id}
          title={t('models.form.editTitle', { name: editingModel.name })}
          initial={modelToForm(editingModel)}
          submitLabel={t('models.form.submitSave')}
          pending={updateModel.isPending}
          errorText={updateModel.isError ? errorMessage(updateModel.error) : null}
          onSubmit={(value) => handleUpdate(editingModel, modelToForm(editingModel), value)}
          onCancel={() => setEditingId(null)}
        />
      )}

      {modelsQuery.isLoading && (
        <div className="flex justify-center py-16">
          <Spinner label={t('models.loading')} />
        </div>
      )}

      {modelsQuery.isError && (
        <ErrorState
          title={t('models.loadError')}
          message={errorMessage(modelsQuery.error)}
          onRetry={() => void modelsQuery.refetch()}
        />
      )}

      {!modelsQuery.isLoading && !modelsQuery.isError && models.length === 0 && (
        <EmptyState title={t('models.empty.title')} message={t('models.empty.message')} />
      )}

      {models.length > 0 && (
        <div className="overflow-x-auto rounded-lg border border-line">
          <table className="w-full text-left">
            <thead>
              <tr className="border-b border-line bg-surface">
                <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
                  {t('models.table.name')}
                </th>
                <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
                  {t('models.table.task')}
                </th>
                <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
                  {t('models.table.endpoint')}
                </th>
                <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
                  {t('models.table.identity')}
                </th>
                <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
                  {t('models.table.secret')}
                </th>
                <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
                  {t('models.table.created')}
                </th>
                <th className="px-3 py-2 text-xs font-medium uppercase text-muted">
                  {t('models.table.actions')}
                </th>
              </tr>
            </thead>
            <tbody>
              {models.map((model) => (
                <ModelRow
                  key={model.id}
                  model={model}
                  isSuperuser={isSuperuser}
                  expanded={expandedId === model.id}
                  onToggleVersions={() =>
                    setExpandedId((current) => (current === model.id ? null : model.id))
                  }
                  onEdit={() => setEditingId(model.id)}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {isSuperuser && <MlPlatformsPanel />}
    </div>
  )
}
