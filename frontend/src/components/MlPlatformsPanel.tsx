import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  useCheckMlPlatform,
  useCreateMlPlatform,
  useDeleteMlPlatform,
  useImportModelVersion,
  useMlPlatforms,
} from '@/api/queries'
import { ApiError } from '@/api/client'
import type {
  MlIdentity,
  MlPlatform,
  MlPlatformCheck,
  MlPlatformConfig,
  MlPlatformCreate,
  MlPlatformKind,
  ModelVersionImport,
} from '@/api/types'
import { BusinessBadge, useBusinessFeature } from '@/components/BusinessBadge'
import { Button } from '@/components/Button'
import { ErrorState } from '@/components/ErrorState'
import { Spinner } from '@/components/Spinner'

/** Identities each kind takes; mirrors `IDENTITIES` in the backend schema. */
export const IDENTITIES: Record<MlPlatformKind, MlIdentity[]> = {
  mlflow: ['none', 'bearer', 'basic'],
  databricks: ['bearer', 'service_principal'],
  azureml: ['managed_identity', 'service_principal'],
}

const KINDS: MlPlatformKind[] = ['mlflow', 'databricks', 'azureml']

const NEEDS_SECRET: MlIdentity[] = ['bearer', 'basic', 'service_principal']

const INPUT_CLASS =
  'w-full rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent'

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.detail ?? error.title
  if (error instanceof Error) return error.message
  return String(error)
}

interface FormValue {
  name: string
  kind: MlPlatformKind
  tracking_uri: string
  identity_type: MlIdentity
  secret_ref: string
  client_id: string
  tenant_id: string
  job_id: string
  ui_url: string
}

const EMPTY_FORM: FormValue = {
  name: '',
  kind: 'mlflow',
  tracking_uri: '',
  identity_type: 'none',
  secret_ref: '',
  client_id: '',
  tenant_id: '',
  job_id: '',
  ui_url: '',
}

/** The request body for a form; fields a kind or identity does not take are left out. */
export function toCreate(value: FormValue): MlPlatformCreate {
  const config: MlPlatformConfig = {}
  const clientId = value.client_id.trim()
  if (clientId && (value.identity_type === 'service_principal' || value.kind === 'azureml')) {
    config.client_id = clientId
  }
  if (value.kind === 'azureml' && value.identity_type === 'service_principal') {
    config.tenant_id = value.tenant_id.trim()
  }
  if (value.kind === 'mlflow' && value.ui_url.trim()) {
    config.ui_url = value.ui_url.trim()
  }
  if (value.kind === 'databricks' && value.job_id.trim()) {
    config.job_id = Number(value.job_id.trim())
  }
  return {
    name: value.name.trim(),
    kind: value.kind,
    tracking_uri: value.tracking_uri.trim(),
    identity_type: value.identity_type,
    secret_ref: NEEDS_SECRET.includes(value.identity_type) ? value.secret_ref.trim() : null,
    config,
  }
}

function RegisterForm({ onDone }: { onDone: () => void }): JSX.Element {
  const { t } = useTranslation('admin')
  const create = useCreateMlPlatform()
  const [value, setValue] = useState<FormValue>(EMPTY_FORM)

  function set<K extends keyof FormValue>(key: K, next: FormValue[K]): void {
    setValue((prev) => ({ ...prev, [key]: next }))
  }

  function changeKind(kind: MlPlatformKind): void {
    setValue((prev) => ({ ...prev, kind, identity_type: IDENTITIES[kind][0] }))
  }

  const needsSecret = NEEDS_SECRET.includes(value.identity_type)
  const showClientId =
    value.identity_type === 'service_principal' || value.identity_type === 'managed_identity'

  return (
    <form
      aria-label={t('mlPlatforms.form.aria')}
      className="mb-4 grid gap-3 rounded-lg border border-line p-4 sm:grid-cols-2"
      onSubmit={(e) => {
        e.preventDefault()
        create.mutate(toCreate(value), {
          onSuccess: () => {
            setValue(EMPTY_FORM)
            onDone()
          },
        })
      }}
    >
      <label className="text-sm text-ink">
        {t('mlPlatforms.form.name')}
        <input
          className={INPUT_CLASS}
          value={value.name}
          onChange={(e) => set('name', e.target.value)}
          required
        />
      </label>
      <label className="text-sm text-ink">
        {t('mlPlatforms.form.kind')}
        <select
          className={INPUT_CLASS}
          value={value.kind}
          onChange={(e) => changeKind(e.target.value as MlPlatformKind)}
        >
          {KINDS.map((kind) => (
            <option key={kind} value={kind}>
              {t(`mlPlatforms.kinds.${kind}`)}
            </option>
          ))}
        </select>
      </label>
      <label className="text-sm text-ink sm:col-span-2">
        {t('mlPlatforms.form.trackingUri')}
        <input
          className={INPUT_CLASS}
          value={value.tracking_uri}
          placeholder={t(`mlPlatforms.uriPlaceholder.${value.kind}`)}
          onChange={(e) => set('tracking_uri', e.target.value)}
          required
        />
      </label>
      <label className="text-sm text-ink">
        {t('mlPlatforms.form.identity')}
        <select
          className={INPUT_CLASS}
          value={value.identity_type}
          onChange={(e) => set('identity_type', e.target.value as MlIdentity)}
        >
          {IDENTITIES[value.kind].map((identity) => (
            <option key={identity} value={identity}>
              {t(`mlPlatforms.identities.${identity}`)}
            </option>
          ))}
        </select>
      </label>
      {needsSecret && (
        <label className="text-sm text-ink">
          {t('mlPlatforms.form.secretRef')}
          <input
            className={INPUT_CLASS}
            value={value.secret_ref}
            placeholder="azurekeyvault://vault/name"
            onChange={(e) => set('secret_ref', e.target.value)}
            required
          />
        </label>
      )}
      {showClientId && (
        <label className="text-sm text-ink">
          {t('mlPlatforms.form.clientId')}
          <input
            className={INPUT_CLASS}
            value={value.client_id}
            onChange={(e) => set('client_id', e.target.value)}
            required={value.identity_type === 'service_principal'}
          />
        </label>
      )}
      {value.kind === 'azureml' && value.identity_type === 'service_principal' && (
        <label className="text-sm text-ink">
          {t('mlPlatforms.form.tenantId')}
          <input
            className={INPUT_CLASS}
            value={value.tenant_id}
            onChange={(e) => set('tenant_id', e.target.value)}
            required
          />
        </label>
      )}
      {value.kind === 'mlflow' && (
        <label className="text-sm text-ink">
          {t('mlPlatforms.form.uiUrl')}
          <input
            className={INPUT_CLASS}
            value={value.ui_url}
            placeholder="http://localhost:5001"
            onChange={(e) => set('ui_url', e.target.value)}
          />
        </label>
      )}
      {value.kind === 'databricks' && (
        <label className="text-sm text-ink">
          {t('mlPlatforms.form.jobId')}
          <input
            className={INPUT_CLASS}
            value={value.job_id}
            inputMode="numeric"
            pattern="[0-9]*"
            onChange={(e) => set('job_id', e.target.value)}
          />
        </label>
      )}
      <p className="text-xs text-muted sm:col-span-2">{t('mlPlatforms.form.secretHint')}</p>
      {create.isError && (
        <p className="text-sm text-danger sm:col-span-2" role="alert">
          {errorMessage(create.error)}
        </p>
      )}
      <div className="flex gap-2 sm:col-span-2">
        <Button type="submit" size="sm" disabled={create.isPending}>
          {t('mlPlatforms.form.submit')}
        </Button>
        <Button type="button" variant="secondary" size="sm" onClick={onDone}>
          {t('mlPlatforms.form.cancel')}
        </Button>
      </div>
    </form>
  )
}

function PlatformRow({ platform }: { platform: MlPlatform }): JSX.Element {
  const { t } = useTranslation('admin')
  const check = useCheckMlPlatform()
  const remove = useDeleteMlPlatform()
  const [result, setResult] = useState<MlPlatformCheck | null>(null)
  const [confirming, setConfirming] = useState(false)

  return (
    <li className="rounded-md border border-line p-3 text-sm" data-testid="ml-platform-row">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="font-medium text-ink">
            {platform.name}{' '}
            <span className="text-xs text-muted">· {t(`mlPlatforms.kinds.${platform.kind}`)}</span>
          </p>
          <p className="break-all text-xs text-muted">
            {platform.tracking_uri} · {t(`mlPlatforms.identities.${platform.identity_type}`)}
            {platform.config.job_id !== undefined &&
              ` · ${t('mlPlatforms.jobIdShort', { id: platform.config.job_id })}`}
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="secondary"
            size="sm"
            disabled={check.isPending}
            onClick={() => {
              setResult(null)
              check.mutate(platform.id, { onSuccess: setResult })
            }}
          >
            {check.isPending ? t('mlPlatforms.checking') : t('mlPlatforms.check')}
          </Button>
          {confirming ? (
            <>
              <Button
                variant="danger"
                size="sm"
                disabled={remove.isPending}
                onClick={() => remove.mutate(platform.id)}
              >
                {t('mlPlatforms.confirmDelete')}
              </Button>
              <Button variant="secondary" size="sm" onClick={() => setConfirming(false)}>
                {t('mlPlatforms.form.cancel')}
              </Button>
            </>
          ) : (
            <Button variant="secondary" size="sm" onClick={() => setConfirming(true)}>
              {t('mlPlatforms.delete')}
            </Button>
          )}
        </div>
      </div>
      {result && (
        <p
          className={`mt-1 text-xs ${result.ok ? 'text-success' : 'text-danger'}`}
          role={result.ok ? 'status' : 'alert'}
        >
          {result.ok
            ? t('mlPlatforms.checkOk', {
                experiments: (result.info?.experiments ?? []).join(', ') || '—',
              })
            : result.messages.join(' ')}
        </p>
      )}
      {check.isError && (
        <p className="mt-1 text-xs text-danger" role="alert">
          {errorMessage(check.error)}
        </p>
      )}
    </li>
  )
}

/** MLflow, Databricks and Azure ML workspaces the platform talks to (API-6). Superusers. */
export function MlPlatformsPanel(): JSX.Element {
  const { t } = useTranslation('admin')
  const platformsQuery = useMlPlatforms()
  const locked = useBusinessFeature('ml_platforms') === false
  const [adding, setAdding] = useState(false)
  const platforms = platformsQuery.data?.items ?? []

  return (
    <section aria-labelledby="ml-platforms-heading" className="mt-10">
      <div className="mb-2 flex items-center justify-between">
        <h2 id="ml-platforms-heading" className="text-lg font-semibold text-ink">
          {t('mlPlatforms.heading')}
          {locked && (
            <span className="ml-2 align-middle">
              <BusinessBadge linked />
            </span>
          )}
        </h2>
        {!adding && !locked && (
          <Button variant="secondary" size="sm" onClick={() => setAdding(true)}>
            {t('mlPlatforms.add')}
          </Button>
        )}
      </div>
      <p className="mb-3 text-sm text-muted">{t('mlPlatforms.intro')}</p>
      {adding && <RegisterForm onDone={() => setAdding(false)} />}
      {platformsQuery.isLoading && <Spinner label={t('mlPlatforms.loading')} />}
      {platformsQuery.isError && (
        <ErrorState
          title={t('mlPlatforms.loadError')}
          message={errorMessage(platformsQuery.error)}
          onRetry={() => void platformsQuery.refetch()}
        />
      )}
      {platformsQuery.data && platforms.length === 0 && (
        <p className="text-sm text-muted">{t('mlPlatforms.empty')}</p>
      )}
      {platforms.length > 0 && (
        <ul className="space-y-2">
          {platforms.map((platform) => (
            <PlatformRow key={platform.id} platform={platform} />
          ))}
        </ul>
      )}
    </section>
  )
}

interface ImportVersionFormProps {
  modelId: string
  onDone: () => void
}

/** Add a model version from an MLflow run or registered model version (API-6, EXP-8). */
export function ImportVersionForm({ modelId, onDone }: ImportVersionFormProps): JSX.Element {
  const { t } = useTranslation('admin')
  const platformsQuery = useMlPlatforms()
  const importVersion = useImportModelVersion(modelId)
  const platforms = platformsQuery.data?.items ?? []
  const [platformId, setPlatformId] = useState('')
  const [source, setSource] = useState<'run' | 'registered'>('run')
  const [runId, setRunId] = useState('')
  const [registeredModel, setRegisteredModel] = useState('')
  const [modelVersion, setModelVersion] = useState('')

  if (platformsQuery.data && platforms.length === 0) {
    return <p className="text-sm text-muted">{t('mlPlatforms.import.noPlatforms')}</p>
  }

  const chosen = platformId || platforms[0]?.id || ''

  return (
    <form
      aria-label={t('mlPlatforms.import.aria')}
      className="mt-2 grid gap-2 rounded-md border border-line p-3 sm:grid-cols-2"
      onSubmit={(e) => {
        e.preventDefault()
        const payload: ModelVersionImport =
          source === 'run'
            ? { ml_platform_id: chosen, run_id: runId.trim() }
            : {
                ml_platform_id: chosen,
                registered_model: registeredModel.trim(),
                model_version: modelVersion.trim(),
              }
        importVersion.mutate(payload, { onSuccess: onDone })
      }}
    >
      <label className="text-sm text-ink">
        {t('mlPlatforms.import.platform')}
        <select
          className={INPUT_CLASS}
          value={chosen}
          onChange={(e) => setPlatformId(e.target.value)}
        >
          {platforms.map((platform) => (
            <option key={platform.id} value={platform.id}>
              {platform.name}
            </option>
          ))}
        </select>
      </label>
      <label className="text-sm text-ink">
        {t('mlPlatforms.import.source')}
        <select
          className={INPUT_CLASS}
          value={source}
          onChange={(e) => setSource(e.target.value as 'run' | 'registered')}
        >
          <option value="run">{t('mlPlatforms.import.byRun')}</option>
          <option value="registered">{t('mlPlatforms.import.byRegistered')}</option>
        </select>
      </label>
      {source === 'run' ? (
        <label className="text-sm text-ink sm:col-span-2">
          {t('mlPlatforms.import.runId')}
          <input
            className={INPUT_CLASS}
            value={runId}
            onChange={(e) => setRunId(e.target.value)}
            required
          />
        </label>
      ) : (
        <>
          <label className="text-sm text-ink">
            {t('mlPlatforms.import.registeredModel')}
            <input
              className={INPUT_CLASS}
              value={registeredModel}
              onChange={(e) => setRegisteredModel(e.target.value)}
              required
            />
          </label>
          <label className="text-sm text-ink">
            {t('mlPlatforms.import.modelVersion')}
            <input
              className={INPUT_CLASS}
              value={modelVersion}
              onChange={(e) => setModelVersion(e.target.value)}
              required
            />
          </label>
        </>
      )}
      <p className="text-xs text-muted sm:col-span-2">{t('mlPlatforms.import.hint')}</p>
      {importVersion.isError && (
        <p className="text-sm text-danger sm:col-span-2" role="alert">
          {errorMessage(importVersion.error)}
        </p>
      )}
      <div className="flex gap-2 sm:col-span-2">
        <Button type="submit" size="sm" disabled={importVersion.isPending || !chosen}>
          {importVersion.isPending
            ? t('mlPlatforms.import.importing')
            : t('mlPlatforms.import.submit')}
        </Button>
        <Button type="button" variant="secondary" size="sm" onClick={onDone}>
          {t('mlPlatforms.form.cancel')}
        </Button>
      </div>
    </form>
  )
}
