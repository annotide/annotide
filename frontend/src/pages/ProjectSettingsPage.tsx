import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  useConnectors,
  useDeleteProject,
  useExtractPdfText,
  useMembers,
  useProject,
  useRebuildCache,
  useUpdateProject,
} from '@/api/queries'
import { ApiError } from '@/api/client'
import { useAuthStore } from '@/lib/store'
import { Spinner } from '@/components/Spinner'
import { ErrorState } from '@/components/ErrorState'
import { Button } from '@/components/Button'
import { SchemaEditor } from '@/components/SchemaEditor'
import { IdpGroupsSettings } from '@/components/IdpGroupsSettings'
import { MembersPanel } from '@/components/MembersPanel'
import { WebhooksPanel } from '@/components/WebhooksPanel'
import { WorkflowSettings } from '@/components/WorkflowSettings'

interface GeneralFormState {
  name: string
  description: string
  source_connector_id: string
  result_connector_id: string
  cache_connector_id: string
  source_prefix: string
  source_glob: string
  /** `settings.companion_extensions` as typed: ".txt, .json" (§5 multimodal). */
  companion_extensions: string
  /** `settings.pdf_mode`: how scans take in PDFs (CONTRACTS "PDF text mode"). */
  pdf_mode: 'layout' | 'text'
}

function parseExtensions(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((part) => part.trim().toLowerCase())
    .filter(Boolean)
    .map((part) => (part.startsWith('.') ? part : `.${part}`))
}

const EMPTY_FORM: GeneralFormState = {
  name: '',
  description: '',
  source_connector_id: '',
  result_connector_id: '',
  cache_connector_id: '',
  source_prefix: '',
  source_glob: '',
  companion_extensions: '',
  pdf_mode: 'layout',
}

export function ProjectSettingsPage(): JSX.Element {
  const { t } = useTranslation(['settings', 'common'])
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()

  const project = useProject(projectId)
  const connectors = useConnectors()
  const membersQuery = useMembers(projectId)
  const currentUser = useAuthStore((s) => s.user)
  const updateProject = useUpdateProject(projectId ?? '')
  const deleteProject = useDeleteProject()
  const rebuildCache = useRebuildCache(projectId ?? '')
  const extractPdfText = useExtractPdfText(projectId ?? '')

  const [form, setForm] = useState<GeneralFormState>(EMPTY_FORM)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [purgeCache, setPurgeCache] = useState(false)
  const [forceExtract, setForceExtract] = useState(false)
  const seeded = useRef(false)

  useEffect(() => {
    if (seeded.current) return
    if (!project.data) return
    seeded.current = true
    setForm({
      name: project.data.name,
      description: project.data.description ?? '',
      source_connector_id: project.data.source_connector_id ?? '',
      result_connector_id: project.data.result_connector_id ?? '',
      cache_connector_id: project.data.cache_connector_id ?? '',
      source_prefix: project.data.source_prefix ?? '',
      source_glob: project.data.source_glob ?? '',
      companion_extensions: Array.isArray(project.data.settings?.companion_extensions)
        ? (project.data.settings.companion_extensions as string[]).join(', ')
        : '',
      pdf_mode: project.data.settings?.pdf_mode === 'text' ? 'text' : 'layout',
    })
  }, [project.data])

  const members = membersQuery.data ?? []
  const currentMember = members.find((m) => m.user_id === currentUser?.id)
  const isOwner = Boolean(currentUser?.is_superuser) || currentMember?.role === 'owner'

  if (!projectId) {
    return <ErrorState title={t('page.missingProjectId')} />
  }

  function handleSave(): void {
    const settings = project.data?.settings ?? {}
    const current = Array.isArray(settings.companion_extensions)
      ? (settings.companion_extensions as string[])
      : []
    const wanted = parseExtensions(form.companion_extensions)
    const currentPdfMode = settings.pdf_mode === 'text' ? 'text' : 'layout'
    // `settings` is replaced whole: send it only when one of its fields changed.
    const settingsChange =
      wanted.join(',') === current.join(',') && form.pdf_mode === currentPdfMode
        ? {}
        : {
            settings: {
              ...settings,
              companion_extensions: wanted.length > 0 ? wanted : null,
              ...(form.pdf_mode !== currentPdfMode && { pdf_mode: form.pdf_mode === 'text' ? 'text' : null }),
            },
          }
    updateProject.mutate({
      ...settingsChange,
      name: form.name,
      description: form.description || null,
      source_connector_id: form.source_connector_id || null,
      result_connector_id: form.result_connector_id || null,
      cache_connector_id: form.cache_connector_id || null,
      source_prefix: form.source_prefix || null,
      source_glob: form.source_glob || null,
    })
  }

  function handleDelete(): void {
    deleteProject.mutate(projectId as string, {
      onSuccess: () => navigate('/'),
    })
  }

  const connectorOptions = connectors.data?.items ?? []

  return (
    <div className="mx-auto w-full max-w-4xl flex-1 px-4 py-8 sm:px-6">
      <Link to={`/projects/${projectId}`} className="text-sm text-accent hover:underline">
        {t('page.backToProject')}
      </Link>

      {project.isLoading && (
        <div className="flex justify-center py-16">
          <Spinner label={t('page.loading')} />
        </div>
      )}

      {project.isError && (
        <ErrorState
          title={t('page.loadError')}
          message={
            project.error instanceof ApiError
              ? (project.error.detail ?? project.error.title)
              : t('page.unknownError')
          }
          onRetry={() => void project.refetch()}
        />
      )}

      {project.data && (
        <>
          <header className="mb-6 mt-2">
            <h1 className="text-xl font-semibold text-ink">
              {t('page.title', { name: project.data.name })}
            </h1>
            {!isOwner && <p className="mt-1 text-sm text-muted">{t('page.readOnlyNotice')}</p>}
          </header>

          <section aria-labelledby="general-heading" className="mb-8">
            <h2 id="general-heading" className="mb-3 text-lg font-semibold text-ink">
              {t('page.generalHeading')}
            </h2>

            <div className="grid gap-3 sm:grid-cols-2">
              <label className="flex flex-col gap-1 text-sm">
                {t('page.nameLabel')}
                <input
                  aria-label="name"
                  value={form.name}
                  disabled={!isOwner}
                  onChange={(event) => setForm((f) => ({ ...f, name: event.target.value }))}
                  className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
                    disabled:opacity-50"
                />
              </label>

              <label className="flex flex-col gap-1 text-sm sm:col-span-2">
                {t('page.descriptionLabel')}
                <textarea
                  aria-label="description"
                  value={form.description}
                  disabled={!isOwner}
                  onChange={(event) => setForm((f) => ({ ...f, description: event.target.value }))}
                  className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
                    disabled:opacity-50"
                />
              </label>

              <label className="flex flex-col gap-1 text-sm">
                {t('page.sourceConnectorLabel')}
                <select
                  aria-label={t('page.sourceConnectorLabel')}
                  value={form.source_connector_id}
                  disabled={!isOwner}
                  onChange={(event) =>
                    setForm((f) => ({ ...f, source_connector_id: event.target.value }))
                  }
                  className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
                    disabled:opacity-50"
                >
                  <option value="">{t('page.none')}</option>
                  {connectorOptions.map((connector) => (
                    <option key={connector.id} value={connector.id}>
                      {connector.name}
                    </option>
                  ))}
                </select>
              </label>

              <label className="flex flex-col gap-1 text-sm">
                {t('page.resultConnectorLabel')}
                <select
                  aria-label={t('page.resultConnectorLabel')}
                  value={form.result_connector_id}
                  disabled={!isOwner}
                  onChange={(event) =>
                    setForm((f) => ({ ...f, result_connector_id: event.target.value }))
                  }
                  className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
                    disabled:opacity-50"
                >
                  <option value="">{t('page.none')}</option>
                  {connectorOptions.map((connector) => (
                    <option key={connector.id} value={connector.id}>
                      {connector.name}
                    </option>
                  ))}
                </select>
              </label>

              <label className="flex flex-col gap-1 text-sm">
                {t('page.cacheConnectorLabel')}
                <select
                  aria-label={t('page.cacheConnectorLabel')}
                  value={form.cache_connector_id}
                  disabled={!isOwner}
                  onChange={(event) =>
                    setForm((f) => ({ ...f, cache_connector_id: event.target.value }))
                  }
                  className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
                    disabled:opacity-50"
                >
                  <option value="">{t('page.cacheSameAsResult')}</option>
                  {connectorOptions.map((connector) => (
                    <option key={connector.id} value={connector.id}>
                      {connector.name}
                    </option>
                  ))}
                </select>
                <span className="text-xs text-muted">{t('page.cacheConnectorHint')}</span>
              </label>

              <label className="flex flex-col gap-1 text-sm">
                {t('page.sourcePrefixLabel')}
                <input
                  aria-label={t('page.sourcePrefixLabel')}
                  value={form.source_prefix}
                  disabled={!isOwner}
                  onChange={(event) => setForm((f) => ({ ...f, source_prefix: event.target.value }))}
                  className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
                    disabled:opacity-50"
                />
              </label>

              <label className="flex flex-col gap-1 text-sm">
                {t('page.sourceGlobLabel')}
                <input
                  aria-label={t('page.sourceGlobLabel')}
                  value={form.source_glob}
                  disabled={!isOwner}
                  onChange={(event) => setForm((f) => ({ ...f, source_glob: event.target.value }))}
                  className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
                    disabled:opacity-50"
                />
              </label>

              <label className="flex flex-col gap-1 text-sm sm:col-span-2">
                {t('page.companionLabel')}
                <input
                  aria-label={t('page.companionLabel')}
                  aria-describedby="companion-hint"
                  placeholder=".txt, .json"
                  value={form.companion_extensions}
                  disabled={!isOwner}
                  onChange={(event) =>
                    setForm((f) => ({ ...f, companion_extensions: event.target.value }))
                  }
                  className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
                    disabled:opacity-50"
                />
                <span id="companion-hint" className="text-xs text-muted">
                  {t('page.companionHint')}
                </span>
              </label>

              <label className="flex flex-col gap-1 text-sm sm:col-span-2">
                {t('page.pdfModeLabel')}
                <select
                  aria-label={t('page.pdfModeLabel')}
                  aria-describedby="pdf-mode-hint"
                  value={form.pdf_mode}
                  disabled={!isOwner}
                  onChange={(event) =>
                    setForm((f) => ({ ...f, pdf_mode: event.target.value === 'text' ? 'text' : 'layout' }))
                  }
                  className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink
                    disabled:opacity-50"
                >
                  <option value="layout">{t('page.pdfModeLayout')}</option>
                  <option value="text">{t('page.pdfModeText')}</option>
                </select>
                <span id="pdf-mode-hint" className="text-xs text-muted">
                  {t('page.pdfModeHint')}
                </span>
              </label>
            </div>

            {isOwner && (
              <div className="mt-4 flex items-center gap-3">
                <Button variant="primary" disabled={updateProject.isPending} onClick={handleSave}>
                  {updateProject.isPending ? t('common:saving') : t('common:save')}
                </Button>
                {updateProject.isSuccess && (
                  <span role="status" className="text-sm text-success">
                    {t('page.saved')}
                  </span>
                )}
                {updateProject.isError && (
                  <span role="alert" className="text-sm text-danger">
                    {updateProject.error instanceof ApiError
                      ? (updateProject.error.detail ?? updateProject.error.title)
                      : t('page.saveError')}
                  </span>
                )}
              </div>
            )}

            {isOwner && (project.data.cache_connector_id || project.data.result_connector_id) && (
              <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-line pt-4">
                <Button
                  disabled={rebuildCache.isPending}
                  onClick={() => rebuildCache.mutate(purgeCache)}
                >
                  {t('page.rebuildCache')}
                </Button>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={purgeCache}
                    onChange={(event) => setPurgeCache(event.target.checked)}
                  />
                  {t('page.rebuildPurge')}
                </label>
                {rebuildCache.isSuccess && (
                  <span role="status" className="text-sm text-success">
                    {t('page.rebuildQueued')}
                  </span>
                )}
                {rebuildCache.isError && (
                  <span role="alert" className="text-sm text-danger">
                    {rebuildCache.error instanceof ApiError
                      ? (rebuildCache.error.detail ?? rebuildCache.error.title)
                      : t('page.rebuildError')}
                  </span>
                )}
              </div>
            )}

            {isOwner && project.data.result_connector_id && project.data.settings?.pdf_mode === 'text' && (
              <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-line pt-4">
                <Button
                  disabled={extractPdfText.isPending}
                  onClick={() => extractPdfText.mutate(forceExtract)}
                >
                  {t('page.extractPdfText')}
                </Button>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={forceExtract}
                    onChange={(event) => setForceExtract(event.target.checked)}
                  />
                  {t('page.extractPdfTextForce')}
                </label>
                {extractPdfText.isSuccess && (
                  <span role="status" className="text-sm text-success">
                    {t('page.extractPdfTextQueued')}
                  </span>
                )}
                {extractPdfText.isError && (
                  <span role="alert" className="text-sm text-danger">
                    {extractPdfText.error instanceof ApiError
                      ? (extractPdfText.error.detail ?? extractPdfText.error.title)
                      : t('page.extractPdfTextError')}
                  </span>
                )}
              </div>
            )}
          </section>

          <WorkflowSettings
            projectId={projectId}
            workflow={project.data.workflow}
            readOnly={!isOwner}
          />

          <SchemaEditor projectId={projectId} readOnly={!isOwner} />

          <MembersPanel projectId={projectId} />

          <IdpGroupsSettings
            projectId={projectId}
            settings={project.data.settings}
            readOnly={!isOwner}
          />

          {isOwner && <WebhooksPanel projectId={projectId} />}

          {isOwner && (
            <section aria-labelledby="danger-heading" className="mb-8">
              <h2 id="danger-heading" className="mb-3 text-lg font-semibold text-ink">
                {t('page.dangerHeading')}
              </h2>
              {!confirmingDelete && (
                <Button variant="danger" onClick={() => setConfirmingDelete(true)}>
                  {t('page.deleteProject')}
                </Button>
              )}
              {confirmingDelete && (
                <div className="flex items-center gap-3">
                  <span className="text-sm text-ink">{t('page.confirmDeletePrompt')}</span>
                  <Button
                    variant="danger"
                    disabled={deleteProject.isPending}
                    onClick={handleDelete}
                  >
                    {deleteProject.isPending ? t('page.deleting') : t('page.confirmDelete')}
                  </Button>
                  <Button variant="secondary" onClick={() => setConfirmingDelete(false)}>
                    {t('common:cancel')}
                  </Button>
                </div>
              )}
              {deleteProject.isError && (
                <p role="alert" className="mt-3 text-sm text-danger">
                  {deleteProject.error instanceof ApiError
                    ? (deleteProject.error.detail ?? deleteProject.error.title)
                    : t('page.deleteError')}
                </p>
              )}
            </section>
          )}
        </>
      )}
    </div>
  )
}
