import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import {
  useAnnotations,
  useConsensus,
  useItem,
  useItemText,
  useProject,
  useReviewAnnotation,
  useSchemas,
  useSignTiles,
} from '@/api/queries'
import type { AnnotationResult, ItemTiles, LabelClass } from '@/api/types'
import type { PageFocus } from '@/features/pdf-annotator'
import { ApiError } from '@/api/client'
import { NoPreview } from '@/components/NoPreview'
import { pdfTextOf } from '@/lib/pdfText'
import { Spinner } from '@/components/Spinner'
import { ErrorState } from '@/components/ErrorState'
import { EmptyState } from '@/components/EmptyState'
import { Button } from '@/components/Button'
import { CommentsPanel } from '@/components/CommentsPanel'
import { AttributeEditor } from '@/components/AttributeEditor'
import { ViewsPanel } from '@/components/ViewsPanel'
import { ConsensusPanel } from '@/components/ConsensusPanel'
import { GoldToggle } from '@/components/GoldToggle'
import { useHotkeys } from '@/lib/hotkeys'
import { useTaskQueue } from '@/lib/useTaskQueue'
import type { ImageAnnotatorProps } from '@/features/annotator'
import { resolveScale } from '@/features/annotator/measure'

// The annotator canvas (Konva) is imported lazily so it loads as its own
// chunk; the page renders a loading fallback until it arrives.
const ImageAnnotator = lazy(() =>
  import('@/features/annotator').then((mod) => ({ default: mod.ImageAnnotator })),
)
import { parseLlmDocument } from '@/features/llm-annotator/shapes'
import { parseSeries } from '@/features/segments/series'

const TextAnnotator = lazy(() =>
  import('@/features/text-annotator').then((mod) => ({ default: mod.TextAnnotator })),
)
const AudioAnnotator = lazy(() =>
  import('@/features/audio-annotator').then((mod) => ({ default: mod.AudioAnnotator })),
)
const TimeSeriesAnnotator = lazy(() =>
  import('@/features/timeseries-annotator').then((mod) => ({ default: mod.TimeSeriesAnnotator })),
)
const LlmAnnotator = lazy(() =>
  import('@/features/llm-annotator').then((mod) => ({ default: mod.LlmAnnotator })),
)
const VideoAnnotator = lazy(() =>
  import('@/features/video-annotator').then((mod) => ({ default: mod.VideoAnnotator })),
)
const PdfAnnotator = lazy(() =>
  import('@/features/pdf-annotator').then((mod) => ({ default: mod.PdfAnnotator })),
)

function errorMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.detail ?? err.title : fallback
}

/** `item.meta.tiles` (IMG-1), narrowed from the untyped JSONB bag. */
function itemTiles(meta: Record<string, unknown>): ItemTiles | null {
  const tiles = meta.tiles
  if (!tiles || typeof tiles !== 'object') return null
  const candidate = tiles as Partial<ItemTiles>
  if (candidate.format !== 'dzi' || typeof candidate.path !== 'string') return null
  if (typeof candidate.max_level !== 'number') return null
  return candidate as ItemTiles
}

export function ReviewPage(): JSX.Element {
  const { t } = useTranslation('annotator')
  const { projectId, itemId } = useParams<{ projectId: string; itemId?: string }>()
  const navigate = useNavigate()

  const [comment, setComment] = useState('')
  const commentRef = useRef<HTMLTextAreaElement>(null)
  // Shape selected on the canvas, to show its attributes read-only (TOOL-2).
  const [selectedId, setSelectedId] = useState<string | null>(null)
  // PDF text mode: the page the PDF beside the text should show, per item.
  const [pdfFocus, setPdfFocus] = useState<(PageFocus & { itemId: string }) | undefined>()
  useEffect(() => setSelectedId(null), [itemId])

  const queue = useTaskQueue({
    projectId,
    type: 'review',
    queueMode: !itemId,
    onClaimed: (task) => navigate(`/projects/${projectId}/review/${task.item_id}`, { replace: true }),
  })

  // Task mode: this item was reached by claiming, so the lock is ours (WF-3).
  // A held task for a different item (URL edited, back button) is not ours to
  // finish here; it is released when the next claim or unmount runs.
  const task = queue.task && queue.task.item_id === itemId ? queue.task : null

  const itemQuery = useItem(itemId)
  const annotationsQuery = useAnnotations(itemId)
  // Signs DZI tile requests for a tiled item's read-only media layer (IMG-1).
  const signTiles = useSignTiles(itemId)
  // The ruler works read-only too (TOOL-8), on the same scale as annotating.
  const projectQuery = useProject(projectId)
  const measureScale = useMemo(
    () => resolveScale(itemQuery.data?.meta, projectQuery.data?.settings),
    [itemQuery.data?.meta, projectQuery.data?.settings],
  )
  const schemasQuery = useSchemas(projectId)
  const reviewAnnotation = useReviewAnnotation(itemId ?? '')

  // Reset the draft comment whenever the current item changes.
  useEffect(() => {
    setComment('')
  }, [itemId])

  const latestSchema = useMemo(() => {
    const versions = schemasQuery.data ?? []
    if (versions.length === 0) return undefined
    return versions.reduce((latest, current) =>
      current.version > latest.version ? current : latest,
    )
  }, [schemasQuery.data])

  const classes: LabelClass[] = latestSchema?.definition.classes ?? []
  const classificationFields = latestSchema?.definition.classification ?? []

  // History is newest first, so the first submitted entry is the newest one.
  // Only the item's own line of work is reviewed (QA-1): consensus and gold
  // versions are resolved or scored, never approved one by one.
  const primaryVersions = useMemo(
    () => (annotationsQuery.data ?? []).filter((v) => (v.kind ?? 'primary') === 'primary'),
    [annotationsQuery.data],
  )
  const annotation = useMemo(
    () => primaryVersions.find((entry) => entry.status === 'submitted'),
    [primaryVersions],
  )
  const latestPrimary = useMemo(
    () =>
      primaryVersions.length === 0
        ? undefined
        : primaryVersions.reduce((a, b) => (b.version > a.version ? b : a)),
    [primaryVersions],
  )
  const hasConsensus = useMemo(
    () =>
      (annotationsQuery.data ?? []).some(
        (v) => v.kind === 'consensus' && v.status === 'submitted',
      ),
    [annotationsQuery.data],
  )
  const consensusQuery = useConsensus(itemId, hasConsensus && !annotation)
  const consensusResults = useMemo(
    () =>
      Object.fromEntries(
        (annotationsQuery.data ?? [])
          .filter((v) => v.kind === 'consensus')
          .map((v) => [v.id, v.result] as const),
      ),
    [annotationsQuery.data],
  )
  // What the canvas shows for a consensus item: the fused preview or one
  // annotator's version.
  const [shown, setShown] = useState<{ key: string; result: AnnotationResult } | null>(null)
  useEffect(() => setShown(null), [itemId])
  const consensusView = consensusQuery.data
  const displayed: AnnotationResult | undefined =
    annotation?.result ??
    (consensusView ? (shown?.result ?? consensusView.preview) : undefined)
  const textQuery = useItemText(
    itemQuery.data?.media_type === 'text' ||
      itemQuery.data?.media_type === 'llm' ||
      itemQuery.data?.media_type === 'timeseries'
      ? itemQuery.data.media_url
      : null,
  )
  // An llm item's document; null when it is not one we can show.
  const llmDocument = useMemo(
    () =>
      itemQuery.data?.media_type === 'llm' && textQuery.data !== undefined
        ? parseLlmDocument(textQuery.data)
        : null,
    [itemQuery.data?.media_type, textQuery.data],
  )
  // A time-series item's parsed CSV (§5), or why it could not be parsed.
  const seriesResult = useMemo(
    () =>
      itemQuery.data?.media_type === 'timeseries' && textQuery.data !== undefined
        ? parseSeries(textQuery.data)
        : null,
    [itemQuery.data?.media_type, textQuery.data],
  )

  const selectedShape = selectedId
    ? displayed?.shapes.find((shape) => shape.id === selectedId)
    : undefined
  const selectedClass = selectedShape
    ? classes.find((cls) => cls.name === selectedShape.class)
    : undefined

  const handleReview = useCallback(
    (approve: boolean) => {
      if (!annotation || !projectId) return
      reviewAnnotation.mutate(
        { id: annotation.id, approve, comment: comment || undefined },
        {
          onSuccess: () => {
            // The server closed the task with the verdict; nothing to release.
            if (task) queue.finish()
            navigate(`/projects/${projectId}/review`)
          },
        },
      )
    },
    [annotation, comment, navigate, projectId, queue, reviewAnnotation, task],
  )

  const handleReleaseAndBack = useCallback(() => {
    void queue.release().then(() => navigate(`/projects/${projectId}/review`))
  }, [navigate, projectId, queue])

  useHotkeys(
    {
      a: () => handleReview(true),
      r: () => commentRef.current?.focus(),
      'mod+enter': () => handleReview(true),
    },
    true,
    { fallback: true },
  )

  if (!projectId) {
    return <ErrorState title={t('shared.missingProjectId')} />
  }

  // Queue mode: no item in the route, we're waiting on a claim.
  if (!itemId) {
    if (queue.claiming) {
      return (
        <div className="flex flex-1 items-center justify-center p-8">
          <Spinner label={t('review.findingTask')} />
        </div>
      )
    }

    if (queue.error) {
      return (
        <div className="flex flex-1 items-center justify-center p-8">
          <ErrorState
            title={t('review.claimError')}
            message={errorMessage(queue.error, t('shared.unknownError'))}
            onRetry={() => queue.claimNext()}
          />
        </div>
      )
    }

    if (queue.empty) {
      return (
        <div className="flex flex-1 items-center justify-center p-8">
          <EmptyState
            title={t('review.nothingToReview')}
            message={t('review.noOpenTasks')}
            action={
              <div className="flex items-center gap-3">
                <Button variant="primary" onClick={() => queue.claimNext()}>
                  {t('shared.checkAgain')}
                </Button>
                <Link to={`/projects/${projectId}`} className="text-sm text-accent underline">
                  {t('shared.backToProject')}
                </Link>
              </div>
            }
          />
        </div>
      )
    }

    return (
      <div className="flex flex-1 items-center justify-center p-8">
        <Spinner label={t('review.loadingTask')} />
      </div>
    )
  }

  if (itemQuery.isLoading || annotationsQuery.isLoading) {
    return (
      <div className="flex flex-1 items-center justify-center p-8">
        <Spinner label={t('shared.loadingItem')} />
      </div>
    )
  }

  if (itemQuery.isError || !itemQuery.data) {
    return (
      <div className="flex flex-1 items-center justify-center p-8">
        <ErrorState
          title={t('shared.itemLoadError')}
          message={errorMessage(itemQuery.error, t('shared.unknownError'))}
          onRetry={() => void itemQuery.refetch()}
        />
      </div>
    )
  }

  if (annotationsQuery.isError) {
    return (
      <div className="flex flex-1 items-center justify-center p-8">
        <ErrorState
          title={t('review.historyLoadError')}
          message={errorMessage(annotationsQuery.error, t('shared.unknownError'))}
          onRetry={() => void annotationsQuery.refetch()}
        />
      </div>
    )
  }

  const item = itemQuery.data
  const history = annotationsQuery.data ?? []

  const lockedUntilLabel = task?.locked_until
    ? new Date(task.locked_until).toLocaleTimeString()
    : null

  return (
    <div className="flex flex-1 flex-col">
      <div className="flex flex-1 flex-col lg:flex-row">
        {/* Centre pane: the read-only annotator canvas */}
        <section className="order-1 flex flex-1 items-center justify-center bg-black/20 p-4">
          {!displayed ? (
            <div className="flex flex-col items-center gap-4 text-center">
              <p className="text-sm text-muted">{t('review.noSubmitted')}</p>
              <Button variant="primary" onClick={handleReleaseAndBack}>
                {t('review.next')}
              </Button>
            </div>
          ) : !item.media_url ? (
            <NoPreview meta={item.meta} fallback={t('review.noPreview')} />
          ) : item.media_type === 'text' ? (
            textQuery.data === undefined ? (
              <Spinner label={t('shared.loadingText')} />
            ) : (
              <Suspense fallback={<Spinner label={t('shared.loadingAnnotator')} />}>
                <TextAnnotator
                  key={shown?.key ?? 'current'}
                  text={textQuery.data}
                  classes={classes}
                  value={displayed}
                  onChange={() => {}}
                  readOnly
                  onSelectionChange={setSelectedId}
                  pages={pdfTextOf(item.meta)?.pages}
                  onPageFocus={(page) => setPdfFocus({ page, itemId: item.id })}
                />
              </Suspense>
            )
          ) : item.media_type === 'audio' ? (
            <Suspense fallback={<Spinner label={t('shared.loadingAnnotator')} />}>
              <AudioAnnotator
                key={shown?.key ?? 'current'}
                mediaUrl={item.media_url}
                sizeBytes={item.size_bytes}
                classes={classes}
                value={displayed}
                onChange={() => {}}
                readOnly
                onSelectionChange={setSelectedId}
              />
            </Suspense>
          ) : item.media_type === 'timeseries' ? (
            textQuery.data === undefined ? (
              <Spinner label={t('shared.loadingText')} />
            ) : seriesResult === null || !seriesResult.ok ? (
              <ErrorState
                title={t('segments.seriesError')}
                message={seriesResult && !seriesResult.ok ? seriesResult.error : t('segments.seriesHint')}
              />
            ) : (
              <Suspense fallback={<Spinner label={t('shared.loadingAnnotator')} />}>
                <TimeSeriesAnnotator
                  key={shown?.key ?? 'current'}
                  series={seriesResult.series}
                  classes={classes}
                  value={displayed}
                  onChange={() => {}}
                  readOnly
                  onSelectionChange={setSelectedId}
                />
              </Suspense>
            )
          ) : item.media_type === 'llm' ? (
            textQuery.data === undefined ? (
              <Spinner label={t('shared.loadingText')} />
            ) : llmDocument === null ? (
              <ErrorState title={t('page.llmDocumentError')} message={t('page.llmDocumentHint')} />
            ) : (
              <Suspense fallback={<Spinner label={t('shared.loadingAnnotator')} />}>
                <LlmAnnotator
                  key={shown?.key ?? 'current'}
                  document={llmDocument}
                  classes={classes}
                  value={displayed}
                  onChange={() => {}}
                  readOnly
                  onSelectionChange={setSelectedId}
                />
              </Suspense>
            )
          ) : item.media_type === 'pdf' ? (
            <Suspense fallback={<Spinner label={t('shared.loadingAnnotator')} />}>
              <PdfAnnotator
                key={shown?.key ?? 'current'}
                pdfUrl={item.media_url}
                classes={classes}
                value={displayed}
                onChange={() => {}}
                readOnly
                onSelectionChange={setSelectedId}
              />
            </Suspense>
          ) : item.media_type === 'video' ? (
            <Suspense fallback={<Spinner label={t('shared.loadingAnnotator')} />}>
              <VideoAnnotator
                key={shown?.key ?? 'current'}
                videoUrl={item.media_url}
                width={item.width ?? 0}
                height={item.height ?? 0}
                fps={typeof item.meta.fps === 'number' ? item.meta.fps : 25}
                classes={classes}
                value={displayed}
                onChange={() => {}}
                readOnly
                onSelectionChange={setSelectedId}
              />
            </Suspense>
          ) : (
            <Suspense fallback={<Spinner label={t('shared.loadingAnnotator')} />}>
              <ImageAnnotator
                key={shown?.key ?? 'current'}
                {...({
                  imageUrl: item.media_url,
                  imageWidth: item.width ?? itemTiles(item.meta)?.width ?? 0,
                  imageHeight: item.height ?? itemTiles(item.meta)?.height ?? 0,
                  classes,
                  value: displayed,
                  onChange: () => {},
                  readOnly: true,
                  onSelectionChange: setSelectedId,
                  measureScale,
                  tiles: itemTiles(item.meta),
                  itemId,
                  signTiles,
                } satisfies ImageAnnotatorProps)}
              />
            </Suspense>
          )}
        </section>

        {/* Right sidebar: item metadata, version history, review form */}
        <aside className="order-2 w-full shrink-0 border-line p-4 lg:w-80 lg:border-l">
          <h2 className="mb-2 text-sm font-semibold text-ink">{t('shared.item')}</h2>
          <dl className="mb-4 space-y-1 text-sm">
            <div className="flex justify-between gap-2">
              <dt className="text-muted">{t('shared.path')}</dt>
              <dd className="truncate text-ink" title={item.path}>
                {item.path}
              </dd>
            </div>
            <div className="flex justify-between gap-2">
              <dt className="text-muted">{t('shared.status')}</dt>
              <dd className="text-ink">{item.status}</dd>
            </div>
          </dl>

          {task && (
            <div className="mb-4 flex items-center justify-between gap-2 rounded-md border border-line p-2 text-xs text-muted">
              {lockedUntilLabel && (
                <span>{t('review.lockedUntil', { time: lockedUntilLabel })}</span>
              )}
              <Button variant="ghost" size="sm" onClick={handleReleaseAndBack}>
                {t('shared.release')}
              </Button>
            </div>
          )}

          <ViewsPanel
            itemId={item.id}
            meta={item.meta}
            pdfFocus={pdfFocus?.itemId === item.id ? pdfFocus : undefined}
          />

          {annotation && classificationFields.length > 0 && (
            <section aria-labelledby="classification-heading" className="mb-4">
              <h2 id="classification-heading" className="mb-2 text-sm font-semibold text-ink">
                {t('shared.classification')}
              </h2>
              <AttributeEditor
                idPrefix="classification"
                fields={classificationFields}
                values={annotation.result.classification}
                onChange={() => {}}
                readOnly
              />
            </section>
          )}

          {selectedShape && (
            <section aria-labelledby="selected-shape-heading" className="mb-4">
              <h2 id="selected-shape-heading" className="mb-2 text-sm font-semibold text-ink">
                {t('shared.selectedShape')}
              </h2>
              <p className="mb-2 text-sm text-ink">
                {selectedClass?.display_name ?? selectedShape.class}{' '}
                <span className="text-xs text-muted">{selectedShape.type}</span>
              </p>
              <AttributeEditor
                idPrefix="shape"
                fields={selectedClass?.attributes ?? []}
                values={selectedShape.attributes}
                onChange={() => {}}
                readOnly
              />
            </section>
          )}

          <h2 className="mb-2 text-sm font-semibold text-ink">{t('review.versions')}</h2>
          {history.length === 0 ? (
            <p className="mb-6 text-sm text-muted">{t('review.noHistory')}</p>
          ) : (
            <ul className="mb-6 space-y-1 text-sm">
              {history.map((entry) => (
                <li key={entry.id} className="rounded px-2 py-1 text-ink">
                  <div className="flex items-center justify-between gap-2">
                    <span>v{entry.version}</span>
                    <span className="text-xs text-muted">{entry.status}</span>
                  </div>
                  <div className="flex items-center justify-between gap-2 text-xs text-muted">
                    <span>{entry.source}</span>
                    <span>{new Date(entry.created_at).toLocaleString()}</span>
                  </div>
                </li>
              ))}
            </ul>
          )}

          {!annotation && consensusView && projectId && itemId && (
            <ConsensusPanel
              itemId={itemId}
              projectId={projectId}
              view={consensusView}
              shown={shown?.key ?? 'fused'}
              onShow={(key, result) => setShown({ key, result })}
              results={consensusResults}
              onResolved={() => {
                if (task) queue.finish()
                navigate(`/projects/${projectId}/review`)
              }}
            />
          )}
          {!annotation && hasConsensus && consensusQuery.isError && (
            <p role="alert" className="mb-4 text-sm text-danger">
              {errorMessage(consensusQuery.error, t('review.consensusError'))}
            </p>
          )}

          <GoldToggle item={item} latest={latestPrimary} />

          {annotation && (
            <>
              <h2 className="mb-2 text-sm font-semibold text-ink">{t('review.review')}</h2>
              <label htmlFor="review-comment" className="mb-1 block text-sm font-medium text-ink">
                Comment
              </label>
              <textarea
                id="review-comment"
                ref={commentRef}
                value={comment}
                onChange={(event) => setComment(event.target.value)}
                rows={4}
                className="mb-1 w-full rounded-md border border-line bg-surface p-2 text-sm text-ink
                  focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
                  focus-visible:outline-accent"
              />
              {comment.length === 0 && (
                <p className="mb-2 text-xs text-muted">A rejection needs a comment</p>
              )}

              {reviewAnnotation.isError && (
                <p className="mb-2 text-sm text-danger">
                  {errorMessage(reviewAnnotation.error, t('review.reviewFailed'))}
                </p>
              )}

              <div className="flex gap-2">
                <Button
                  variant="primary"
                  onClick={() => handleReview(true)}
                  disabled={reviewAnnotation.isPending}
                >
                  Approve
                </Button>
                <Button
                  variant="danger"
                  onClick={() => handleReview(false)}
                  disabled={reviewAnnotation.isPending || comment.length === 0}
                >
                  Reject
                </Button>
              </div>
            </>
          )}

          <h2 className="mb-2 mt-6 text-sm font-semibold text-ink">{t('comments.title')}</h2>
          <CommentsPanel itemId={itemId} annotationId={annotation?.id} />
        </aside>
      </div>
    </div>
  )
}
