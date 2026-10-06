import { useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useJobs, useSchemas, useUploadImport } from '@/api/queries'
import { ApiError } from '@/api/client'
import type { ImportFormat, ImportResult, Job } from '@/api/types'
import { Button } from '@/components/Button'
import { JobRetryButton } from '@/components/JobRetryButton'
import { ErrorState } from '@/components/ErrorState'
import { Spinner } from '@/components/Spinner'

/** Sentinel select value meaning "drop this attribute" (maps to `null`). */
const DISCARD = '__discard__'
/** Sentinel select value meaning "keep the source name" (identity, the default). */
const SAME_NAME = ''

const FORMAT_OPTIONS: Array<{ value: ImportFormat; label: string }> = [
  { value: 'coco', label: 'COCO' },
  { value: 'yolo', label: 'YOLO' },
  { value: 'voc', label: 'Pascal VOC' },
  { value: 'cvat', label: 'CVAT (images)' },
  { value: 'label_studio', label: 'Label Studio' },
]

const SELECT_CLASS =
  'rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink ' +
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 ' +
  'focus-visible:outline-accent'

function isInFlight(job: Job): boolean {
  return job.status === 'queued' || job.status === 'running'
}

/** Parse `{"source": "schema"}` from the mapping textarea; `null` when it is not an object. */
export function parseClassMapping(text: string): Record<string, string> | null {
  const trimmed = text.trim()
  if (!trimmed) return {}
  try {
    const parsed: unknown = JSON.parse(trimmed)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
    return Object.fromEntries(
      Object.entries(parsed as Record<string, unknown>).map(([key, value]) => [key, String(value)]),
    )
  } catch {
    return null
  }
}

function ImportSummary({ result }: { result: ImportResult }): JSX.Element {
  const { t } = useTranslation('projects')
  return (
    <div className="mt-1 text-xs text-muted">
      <p>
        {result.dry_run ? t('import.summary.dryRunPrefix') : ''}
        {t('import.summary.counts', {
          imported: result.imported,
          matched: result.matched,
          parsed: result.parsed,
        })}
        {result.unmatched > 0 && t('import.summary.unmatchedCount', { count: result.unmatched })}
        {result.errors > 0 && t('import.summary.errors', { count: result.errors })}
        {result.dropped_shapes > 0 &&
          t('import.summary.droppedShapes', { count: result.dropped_shapes })}
        {(result.dropped_attributes ?? 0) > 0 &&
          t('import.summary.droppedAttributes', { count: result.dropped_attributes })}
      </p>
      {Object.keys(result.classes).length > 0 && (
        <p>
          {t('import.summary.classes', {
            list: Object.entries(result.classes)
              .map(([name, count]) => `${name} (${count})`)
              .join(', '),
          })}
        </p>
      )}
      {result.unmatched_sample.length > 0 && (
        <p>{t('import.summary.unmatchedSample', { list: result.unmatched_sample.join(', ') })}</p>
      )}
      {result.problems.length > 0 && (
        <ul className="mt-1 list-disc pl-4 text-warning">
          {result.problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

/**
 * Upload an annotation file or archive and queue an import job (EXP-6).
 *
 * Owner-only on the API side; the panel lists the last import jobs and
 * shows each finished job's counts so a dry run reads like a preview.
 */
export function ImportPanel({ projectId }: { projectId: string }): JSX.Element {
  const { t } = useTranslation('projects')
  const [format, setFormat] = useState<ImportFormat>('coco')
  const [status, setStatus] = useState<'submitted' | 'draft'>('submitted')
  const [dryRun, setDryRun] = useState(false)
  const [mappingText, setMappingText] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  // Keyed by source class, then source attribute; value is the sentinel
  // SAME_NAME / DISCARD, or a target attribute name.
  const [attributeOverrides, setAttributeOverrides] = useState<Record<string, Record<string, string>>>(
    {},
  )
  const fileInput = useRef<HTMLInputElement>(null)

  const upload = useUploadImport(projectId)
  const jobsQuery = useJobs(
    projectId,
    { type: 'import', limit: 10 },
    {
      refetchInterval: (query) => {
        const jobs = query.state.data?.items ?? []
        return jobs.some(isInFlight) ? 3000 : false
      },
    },
  )
  const schemasQuery = useSchemas(projectId)

  const hint = t(`import.formatHints.${format}`)

  // Newest schema version first (see listSchemas); its classes are what a
  // mapped class's attributes can be renamed or discarded into.
  const schemaClasses = schemasQuery.data?.[0]?.definition.classes ?? []
  const classMappingForDisplay = parseClassMapping(mappingText) ?? {}

  // The most recent dry-run job that reported source attributes, i.e. the
  // preview the attribute mapping editor below is built from.
  const latestDryRunResult = useMemo<ImportResult | null>(() => {
    const jobs = jobsQuery.data?.items ?? []
    const withAttributes = jobs.find((job) => {
      const result = job.result as Partial<ImportResult> | null
      return job.status === 'succeeded' && Boolean(result?.dry_run) && Boolean(result?.attributes)
    })
    return (withAttributes?.result as unknown as ImportResult) ?? null
  }, [jobsQuery.data])
  const sourceAttributesByClass = latestDryRunResult?.attributes ?? {}
  // Source class names (before mapping) with their shape counts, from the
  // same dry run: the rows of the class mapping editor.
  const sourceClassCounts = latestDryRunResult?.classes ?? {}
  const schemaClassNames = new Set(schemaClasses.map((cls) => cls.name))

  /** Set one source class's target in the JSON mapping; `SAME_NAME` removes it. */
  function setClassTarget(sourceClass: string, target: string): void {
    const current = parseClassMapping(mappingText) ?? {}
    const next = { ...current }
    if (target === SAME_NAME) delete next[sourceClass]
    else next[sourceClass] = target
    setMappingText(Object.keys(next).length > 0 ? JSON.stringify(next, null, 2) : '')
  }

  function targetClassFor(sourceClass: string): string {
    return classMappingForDisplay[sourceClass] ?? sourceClass
  }

  function targetAttributeNames(sourceClass: string): string[] {
    const targetClass = targetClassFor(sourceClass)
    return schemaClasses.find((cls) => cls.name === targetClass)?.attributes.map((a) => a.name) ?? []
  }

  function setAttributeOverride(sourceClass: string, sourceAttribute: string, value: string): void {
    setAttributeOverrides((prev) => ({
      ...prev,
      [sourceClass]: { ...prev[sourceClass], [sourceAttribute]: value },
    }))
  }

  /** Only non-identity choices are sent, keyed by each attribute's *target* class. */
  function buildAttributeMapping(): Record<string, Record<string, string | null>> {
    const mapping: Record<string, Record<string, string | null>> = {}
    for (const [sourceClass, sourceAttributes] of Object.entries(sourceAttributesByClass)) {
      const overridesForClass = attributeOverrides[sourceClass]
      if (!overridesForClass) continue
      const targetClass = targetClassFor(sourceClass)
      for (const sourceAttribute of sourceAttributes) {
        const selection = overridesForClass[sourceAttribute]
        if (!selection || selection === SAME_NAME) continue
        mapping[targetClass] = {
          ...mapping[targetClass],
          [sourceAttribute]: selection === DISCARD ? null : selection,
        }
      }
    }
    return mapping
  }

  function submit(): void {
    setFormError(null)
    if (!file) {
      setFormError(t('import.chooseFile'))
      return
    }
    const classMapping = parseClassMapping(mappingText)
    if (classMapping === null) {
      setFormError(t('import.invalidMapping'))
      return
    }
    const attributeMapping = buildAttributeMapping()
    upload.mutate(
      {
        file,
        options: {
          format,
          status,
          dry_run: dryRun,
          class_mapping: classMapping,
          ...(Object.keys(attributeMapping).length > 0
            ? { attribute_mapping: attributeMapping }
            : {}),
        },
      },
      {
        onSuccess: () => {
          setFile(null)
          if (fileInput.current) fileInput.current.value = ''
        },
      },
    )
  }

  return (
    <section aria-labelledby="imports-heading" className="mb-8">
      <h2 id="imports-heading" className="mb-3 text-lg font-semibold text-ink">
        {t('import.heading')}
      </h2>

      <div className="mb-3 flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1">
          <label htmlFor="import-format" className="text-sm font-medium text-ink">
            {t('import.format')}
          </label>
          <select
            id="import-format"
            value={format}
            onChange={(event) => setFormat(event.target.value as ImportFormat)}
            className={SELECT_CLASS}
          >
            {FORMAT_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor="import-file" className="text-sm font-medium text-ink">
            {t('import.file')} {hint && <span className="font-normal text-muted">({hint})</span>}
          </label>
          <input
            id="import-file"
            ref={fileInput}
            type="file"
            accept=".json,.xml,.zip,.txt,.yaml,.yml"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            className="max-w-full text-sm text-ink"
          />
        </div>

        <div className="flex flex-col gap-1">
          <label htmlFor="import-status" className="text-sm font-medium text-ink">
            {t('import.writeAs')}
          </label>
          <select
            id="import-status"
            value={status}
            onChange={(event) => setStatus(event.target.value as 'submitted' | 'draft')}
            className={SELECT_CLASS}
          >
            <option value="submitted">{t('import.writeAsSubmitted')}</option>
            <option value="draft">{t('import.writeAsDraft')}</option>
          </select>
        </div>

        <label className="flex items-center gap-2 text-sm text-ink">
          <input
            type="checkbox"
            checked={dryRun}
            onChange={(event) => setDryRun(event.target.checked)}
          />
          {t('import.dryRun')}
        </label>

        <Button variant="primary" disabled={upload.isPending} onClick={submit}>
          {upload.isPending ? t('import.uploading') : dryRun ? t('import.preview') : t('import.submit')}
        </Button>
      </div>

      {upload.isPending && (
        <div className="mb-3 max-w-md">
          <div
            role="progressbar"
            aria-label={t('import.progressLabel')}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={Math.round((upload.progress ?? 0) * 100)}
            className="h-2 w-full overflow-hidden rounded-full bg-line"
          >
            <div
              className="h-full bg-accent transition-[width]"
              style={{ width: `${Math.round((upload.progress ?? 0) * 100)}%` }}
            />
          </div>
        </div>
      )}

      {Object.keys(sourceClassCounts).length > 0 && (
        <fieldset className="mb-3 max-w-md rounded-md border border-line p-3">
          <legend className="px-1 text-sm font-medium text-ink">
            {t('import.classMapping.legend')}{' '}
            <span className="font-normal text-muted">{t('import.classMapping.fromDryRun')}</span>
          </legend>
          <div className="flex flex-col gap-1">
            {Object.entries(sourceClassCounts).map(([sourceClass, count]) => {
              const selectId = `class-map-${sourceClass}`
              const mapped = classMappingForDisplay[sourceClass]
              const target = mapped ?? sourceClass
              const dropped = !schemaClassNames.has(target)
              return (
                <div key={sourceClass} className="flex flex-wrap items-center gap-2 py-0.5">
                  <label htmlFor={selectId} className="w-40 max-w-full truncate text-sm text-ink">
                    {sourceClass} <span className="text-xs text-muted">({count})</span>
                  </label>
                  <select
                    id={selectId}
                    value={mapped ?? SAME_NAME}
                    onChange={(event) => setClassTarget(sourceClass, event.target.value)}
                    className={SELECT_CLASS}
                  >
                    <option value={SAME_NAME}>{t('import.classMapping.sameName')}</option>
                    {schemaClasses.map((cls) => (
                      <option key={cls.name} value={cls.name}>
                        {cls.display_name || cls.name}
                      </option>
                    ))}
                  </select>
                  {dropped && (
                    <span className="text-xs text-danger">
                      {t('import.classMapping.notInSchema')}
                    </span>
                  )}
                </div>
              )
            })}
          </div>
        </fieldset>
      )}

      <details className="mb-3" open={Object.keys(sourceClassCounts).length === 0 || undefined}>
        <summary className="cursor-pointer text-sm font-medium text-ink">
          {Object.keys(sourceClassCounts).length > 0
            ? t('import.classMapping.editAsJson')
            : t('import.classMapping.optionalJson')}
        </summary>
        <label htmlFor="import-mapping" className="sr-only">
          {t('import.classMapping.jsonLabel')}
        </label>
        <textarea
          id="import-mapping"
          value={mappingText}
          onChange={(event) => setMappingText(event.target.value)}
          placeholder='{"automobile": "car"}'
          rows={3}
          className={`${SELECT_CLASS} mt-1 w-full max-w-md font-mono`}
        />
      </details>

      {Object.keys(sourceAttributesByClass).length > 0 && (
        <fieldset className="mb-3 max-w-md rounded-md border border-line p-3">
          <legend className="px-1 text-sm font-medium text-ink">
            {t('import.attributeMapping.legend')}{' '}
            <span className="font-normal text-muted">
              {t('import.attributeMapping.fromDryRun')}
            </span>
          </legend>
          <div className="flex flex-col gap-2">
            {Object.entries(sourceAttributesByClass).map(([sourceClass, sourceAttributes]) => (
              <div key={sourceClass}>
                <p className="text-xs font-semibold text-muted">
                  {sourceClass} → {targetClassFor(sourceClass)}
                </p>
                {sourceAttributes.map((sourceAttribute) => {
                  const selectId = `attr-map-${sourceClass}-${sourceAttribute}`
                  const value = attributeOverrides[sourceClass]?.[sourceAttribute] ?? SAME_NAME
                  return (
                    <div key={sourceAttribute} className="flex flex-wrap items-center gap-2 py-0.5">
                      <label htmlFor={selectId} className="w-40 max-w-full truncate text-sm text-ink">
                        {sourceAttribute}
                      </label>
                      <select
                        id={selectId}
                        value={value}
                        onChange={(event) =>
                          setAttributeOverride(sourceClass, sourceAttribute, event.target.value)
                        }
                        className={SELECT_CLASS}
                      >
                        <option value={SAME_NAME}>{t('import.attributeMapping.sameName')}</option>
                        <option value={DISCARD}>{t('import.attributeMapping.discard')}</option>
                        {targetAttributeNames(sourceClass).map((name) => (
                          <option key={name} value={name}>
                            {name}
                          </option>
                        ))}
                      </select>
                    </div>
                  )
                })}
              </div>
            ))}
          </div>
        </fieldset>
      )}

      {formError && <p className="mb-3 text-sm text-danger">{formError}</p>}
      {upload.isError && (
        <p className="mb-3 text-sm text-danger">
          {upload.error instanceof ApiError
            ? upload.error.detail ?? upload.error.title
            : t('import.uploadError')}
        </p>
      )}

      {jobsQuery.isLoading && (
        <div className="flex justify-center py-6">
          <Spinner label={t('import.loading')} />
        </div>
      )}

      {jobsQuery.isError && (
        <ErrorState
          title={t('import.loadError')}
          message={
            jobsQuery.error instanceof ApiError
              ? jobsQuery.error.detail ?? jobsQuery.error.title
              : t('import.unknownError')
          }
          onRetry={() => void jobsQuery.refetch()}
        />
      )}

      {jobsQuery.data && jobsQuery.data.items.length === 0 && (
        <p className="text-sm text-muted">{t('import.empty')}</p>
      )}

      {jobsQuery.data && jobsQuery.data.items.length > 0 && (
        <ul className="space-y-2">
          {jobsQuery.data.items.map((job) => (
            <li key={job.id} className="rounded-md border border-line p-3 text-sm">
              <p className="text-ink">
                {String(job.payload.format)} · {job.status} · {job.progress}%
              </p>
              <p className="text-xs text-muted">{new Date(job.created_at).toLocaleString()}</p>
              {job.status === 'failed' && job.error && (
                <p className="text-xs text-danger">{job.error}</p>
              )}
              <JobRetryButton job={job} />
              {job.status === 'succeeded' && job.result && (
                <ImportSummary result={job.result as unknown as ImportResult} />
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
