import { useCallback, useEffect, useRef, useState, type DragEvent } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useJob, useUploadFiles, type UploadEntry, type UploadOutcome } from '@/api/queries'
import { ApiError } from '@/api/client'
import type { Job } from '@/api/types'
import { Button } from '@/components/Button'
import { ErrorState } from '@/components/ErrorState'
import i18n from '@/i18n'

/**
 * Upload media from the browser straight to the project's source connector
 * (§12 upload path). The API only mints signed URLs; bytes go to the store.
 * After the PUTs a scan is queued, which is what actually creates the items.
 *
 * Accepts single files, a multi-select, or a whole folder — from the picker
 * (`webkitdirectory`) or drag-and-drop — and keeps relative paths so a
 * dropped `photos/2024/a.jpg` lands under the same folder in the store.
 */

/** Relative path for one picked file: the folder-relative path when a folder was chosen. */
export function entryPath(file: File): string {
  const relative = (file as File & { webkitRelativePath?: string }).webkitRelativePath
  return relative && relative.length > 0 ? relative : file.name
}

/**
 * Walk a dropped directory tree through the non-standard but universal
 * `webkitGetAsEntry` API. Falls back to the flat file list when the browser
 * has no entry API, in which case folders are silently skipped.
 */
export async function collectDroppedEntries(dataTransfer: DataTransfer): Promise<UploadEntry[]> {
  const items = Array.from(dataTransfer.items ?? [])
  const roots = items
    .map((item) => (typeof item.webkitGetAsEntry === 'function' ? item.webkitGetAsEntry() : null))
    .filter((entry): entry is FileSystemEntry => entry !== null)
  if (roots.length === 0) {
    return Array.from(dataTransfer.files).map((file) => ({ path: entryPath(file), file }))
  }

  const out: UploadEntry[] = []
  async function walk(entry: FileSystemEntry, prefix: string): Promise<void> {
    if (entry.isFile) {
      const fileEntry = entry as FileSystemFileEntry
      const file = await new Promise<File>((ok, err) => fileEntry.file(ok, err))
      out.push({ path: `${prefix}${entry.name}`, file })
      return
    }
    if (entry.isDirectory) {
      const reader = (entry as FileSystemDirectoryEntry).createReader()
      // readEntries returns batches (Chrome caps at 100); loop until empty.
      for (;;) {
        const batch = await new Promise<FileSystemEntry[]>((ok, err) =>
          reader.readEntries(ok, err),
        )
        if (batch.length === 0) break
        for (const child of batch) await walk(child, `${prefix}${entry.name}/`)
      }
    }
  }
  for (const root of roots) await walk(root, '')
  return out
}

/** How often the panel polls the scan it queued, until the scan settles. */
const SCAN_POLL_MS = 2000

function isInFlight(job: Job): boolean {
  return job.status === 'queued' || job.status === 'running'
}

function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.problem.detail ?? error.message
  if (error instanceof Error) return error.message
  return i18n.t('projects:upload.uploadFailed')
}

interface UploadPanelProps {
  projectId: string
}

export function UploadPanel({ projectId }: UploadPanelProps): JSX.Element {
  const { t } = useTranslation('projects')
  const [entries, setEntries] = useState<UploadEntry[]>([])
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null)
  const [outcome, setOutcome] = useState<UploadOutcome | null>(null)
  const [dragging, setDragging] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  const folderInput = useRef<HTMLInputElement>(null)
  const upload = useUploadFiles(projectId)
  const queryClient = useQueryClient()

  // The scan, not the PUTs, creates the items: follow it and refresh the
  // item grid once it has finished.
  const scanJob = useJob(outcome?.job?.id, {
    refetchInterval: (query) =>
      query.state.data && !isInFlight(query.state.data) ? false : SCAN_POLL_MS,
  })
  const scanStatus = scanJob.data?.status ?? outcome?.job?.status
  useEffect(() => {
    if (scanStatus === 'succeeded') {
      void queryClient.invalidateQueries({ queryKey: ['projects', projectId, 'items'] })
    }
  }, [scanStatus, projectId, queryClient])

  const pick = useCallback((list: FileList | null) => {
    if (!list) return
    setOutcome(null)
    setEntries(Array.from(list).map((file) => ({ path: entryPath(file), file })))
  }, [])

  async function onDrop(event: DragEvent<HTMLDivElement>): Promise<void> {
    event.preventDefault()
    setDragging(false)
    setOutcome(null)
    setEntries(await collectDroppedEntries(event.dataTransfer))
  }

  function reset(): void {
    setEntries([])
    setProgress(null)
    if (fileInput.current) fileInput.current.value = ''
    if (folderInput.current) folderInput.current.value = ''
  }

  function submit(): void {
    if (entries.length === 0) return
    setOutcome(null)
    setProgress({ done: 0, total: entries.length })
    upload.mutate(
      { files: entries, onProgress: (done, total) => setProgress({ done, total }) },
      {
        onSuccess: (result) => {
          setOutcome(result)
          reset()
        },
        onError: () => setProgress(null),
      },
    )
  }

  const totalBytes = entries.reduce((sum, entry) => sum + entry.file.size, 0)

  return (
    <section aria-labelledby="upload-heading" className="mb-8">
      <h2 id="upload-heading" className="mb-3 text-lg font-semibold text-ink">
        {t('upload.heading')}
      </h2>

      <div
        role="region"
        aria-label={t('upload.dropZoneLabel')}
        onDragOver={(event) => {
          event.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => void onDrop(event)}
        className={
          'mb-3 rounded-md border border-dashed p-4 text-sm ' +
          (dragging ? 'border-accent bg-accent/5' : 'border-line')
        }
      >
        <p className="mb-2 text-muted">{t('upload.dropHint')}</p>
        <div className="flex flex-wrap items-center gap-3">
          <label className="max-w-full text-sm text-ink">
            <span className="sr-only">{t('upload.filesLabel')}</span>
            <input
              ref={fileInput}
              type="file"
              multiple
              accept="image/*"
              aria-label={t('upload.chooseFiles')}
              onChange={(event) => pick(event.target.files)}
              className="max-w-full text-sm text-ink"
            />
          </label>
          <label className="max-w-full text-sm text-ink">
            <span className="sr-only">{t('upload.folderLabel')}</span>
            <input
              ref={folderInput}
              type="file"
              multiple
              aria-label={t('upload.chooseFolder')}
              onChange={(event) => pick(event.target.files)}
              className="max-w-full text-sm text-ink"
              // Non-standard attributes; React passes them through as-is.
              {...{ webkitdirectory: '', directory: '' }}
            />
          </label>
          <Button
            variant="primary"
            disabled={entries.length === 0 || upload.isPending}
            onClick={submit}
          >
            {upload.isPending
              ? t('upload.uploading', {
                  done: progress?.done ?? 0,
                  total: progress?.total ?? entries.length,
                })
              : entries.length > 0
                ? t('upload.uploadButton', { count: entries.length })
                : t('upload.uploadButtonEmpty')}
          </Button>
          {entries.length > 0 && !upload.isPending && (
            <Button variant="secondary" onClick={reset}>
              {t('upload.clear')}
            </Button>
          )}
        </div>
      </div>

      {entries.length > 0 && (
        <p className="mb-2 text-xs text-muted" data-testid="upload-selection">
          {t('upload.selection', { count: entries.length })}
          {t('upload.selectionSize', { size: (totalBytes / (1024 * 1024)).toFixed(1) })}
          {entries.length <= 5 &&
            t('upload.selectionPaths', { paths: entries.map((entry) => entry.path).join(', ') })}
        </p>
      )}

      {upload.isPending && progress && (
        <progress
          aria-label={t('upload.progressLabel')}
          className="mb-2 h-2 w-full"
          value={progress.done}
          max={progress.total}
        />
      )}

      {upload.isError && <ErrorState message={describeError(upload.error)} />}

      {outcome && (
        <p className="text-sm text-ink" role="status">
          {t('upload.outcome.uploaded', { count: outcome.uploaded })}
          {outcome.failed.length > 0 &&
            t('upload.outcome.failed', { count: outcome.failed.length })}
          {outcome.job &&
            (scanStatus === 'succeeded'
              ? t('upload.outcome.scanDone')
              : scanStatus === 'failed' || scanStatus === 'cancelled'
                ? t('upload.outcome.scanFailed')
                : t('upload.outcome.scanQueued'))}
          {outcome.failed.length > 0 && (
            <span className="block text-xs text-warning">
              {t('upload.outcome.failedList', { list: outcome.failed.slice(0, 5).join(', ') })}
              {outcome.failed.length > 5 &&
                t('upload.outcome.failedMore', { count: outcome.failed.length - 5 })}
            </span>
          )}
        </p>
      )}
    </section>
  )
}
