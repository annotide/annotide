import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { Trans, useTranslation } from 'react-i18next'
import {
  useAnnotations,
  useCreateAnnotation,
  useInteractiveSegment,
  useItem,
  useItemOcr,
  useItemText,
  useItems,
  useModels,
  useProject,
  useSchemas,
  useSignTiles,
  useUpdateProject,
} from '@/api/queries'
import type { AnnotationResult, AttributeValue, ItemTiles, LabelClass } from '@/api/types'
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
import { ShortcutsDialog } from '@/components/ShortcutsDialog'
import type { ShortcutGroup } from '@/components/ShortcutsDialog'
import { useHotkeys } from '@/lib/hotkeys'
import { useRecentItemStore } from '@/lib/store'
import { copyAnnotations } from '@/features/annotator/copy'
import { TOOLS } from '@/features/annotator/toolHotkeys'
import { isAnchorInRegion } from '@/features/annotator/geometry'
import { missingRequiredAttributes } from '@/features/annotator/required'
import { describeShape, resolveScale } from '@/features/annotator/measure'
import type { PixelScale } from '@/features/annotator/measure'
import { useTaskQueue } from '@/lib/useTaskQueue'
import type {
  ImageAnnotatorProps,
  Point2D,
  SelectRequest,
  SmartPrompt,
} from '@/features/annotator'

// The annotator canvas (src/features/annotator) is being built by a separate
// agent in parallel. Importing it lazily lets this page typecheck and render
// (with a loading fallback) even before that module exists or while it is
// still being edited, instead of a hard build-time dependency.
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

const EMPTY_RESULT: AnnotationResult = {
  schema_version: 1,
  media_type: 'image',
  classification: {},
  shapes: [],
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? (error.detail ?? error.title) : fallback
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

export function AnnotatePage(): JSX.Element {
  const { t } = useTranslation(['annotator', 'common'])
  const { projectId, itemId } = useParams<{ projectId: string; itemId?: string }>()
  const navigate = useNavigate()

  const itemsQuery = useItems(projectId, { limit: 100 })
  const itemQuery = useItem(itemId)
  const schemasQuery = useSchemas(projectId)

  // The local draft is tagged with the item it belongs to, so a navigation
  // shows an empty result in the same render rather than one effect later —
  // the annotator's undo history would otherwise start with the previous
  // item's shapes (see the resync note in useDrawing).
  const [draft, setDraft] = useState<{ itemId?: string; result: AnnotationResult }>({
    result: EMPTY_RESULT,
  })
  // Until the first local edit, the item opens on its latest version: a model
  // draft from a prelabel run (ML-2), a saved draft, or the rejected version
  // coming back from review. The annotator is only mounted once these have
  // loaded, so its undo history starts at that version instead of undoing
  // back to an empty canvas.
  const annotationsQuery = useAnnotations(itemId)
  const mediaType = itemQuery.data?.media_type ?? 'image'
  const latestResult = useMemo(() => {
    const versions = annotationsQuery.data ?? []
    // The item's own line of work first (QA-1): an owner opening an item must
    // not start from someone's consensus attempt. A consensus or gold
    // annotator only ever receives their own versions (blind listing), so
    // for them the fallback picks up their own work.
    const primary = versions.filter((v) => (v.kind ?? 'primary') === 'primary')
    const pool = primary.length > 0 ? primary : versions
    if (pool.length === 0) return { ...EMPTY_RESULT, media_type: mediaType }
    return pool.reduce((a, b) => (b.version > a.version ? b : a)).result
  }, [annotationsQuery.data, mediaType])
  const result = draft.itemId === itemId ? draft.result : latestResult
  const setResult = useCallback(
    (next: AnnotationResult) => setDraft({ itemId, result: next }),
    [itemId],
  )
  // What the server last accepted for this item, once saved in this session;
  // before that the opened version is the baseline. Unsaved work is a draft
  // that differs from it — compared by value, so undoing back counts as clean.
  const savedRef = useRef<{ itemId?: string; result: AnnotationResult } | null>(null)
  const hasUnsavedChanges = useCallback((): boolean => {
    if (draft.itemId !== itemId) return false
    const saved = savedRef.current
    const baseline = saved && saved.itemId === itemId ? saved.result : latestResult
    return JSON.stringify(draft.result) !== JSON.stringify(baseline)
  }, [draft, itemId, latestResult])
  const unsavedRef = useRef(hasUnsavedChanges)
  unsavedRef.current = hasUnsavedChanges
  // Leaving the item drops the draft; ask first rather than lose shapes.
  const confirmLeave = useCallback(
    (): boolean => !hasUnsavedChanges() || window.confirm(t('page.unsavedConfirm')),
    [hasUnsavedChanges, t],
  )
  useEffect(() => {
    // Reload or closing the tab: the browser shows its own generic prompt.
    const onBeforeUnload = (event: BeforeUnloadEvent): void => {
      if (unsavedRef.current()) event.preventDefault()
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [])
  // Shape selected on the canvas, for the attribute editor (TOOL-2).
  const [selectedId, setSelectedId] = useState<string | null>(null)
  // PDF text mode: the page the PDF beside the text should show, per item.
  const [pdfFocus, setPdfFocus] = useState<(PageFocus & { itemId: string }) | undefined>()
  const [selectRequest, setSelectRequest] = useState<SelectRequest | null>(null)
  // When the current item was opened, for `duration_ms` on submit.
  const openedAt = useRef(Date.now())
  // Set by a submit that stopped on missing required attributes (QA-6), so
  // the notice shows only after an attempt and follows the edits after it.
  const [submitBlocked, setSubmitBlocked] = useState(false)
  // Signs DZI tile requests for a tiled item's media layer (IMG-1).
  const signTiles = useSignTiles(itemId)

  // Smart polygon (ML-7): any `segment` model of the organisation can drive
  // the tool; the first one is the default and a picker appears past that.
  const modelsQuery = useModels()
  const segmentModels = useMemo(
    () =>
      (modelsQuery.data?.items ?? []).filter(
        (model) => model.task === 'segment' && model.endpoint_url !== null,
      ),
    [modelsQuery.data],
  )
  const [chosenSmartModelId, setChosenSmartModelId] = useState<string | null>(null)
  const smartModelId =
    segmentModels.find((model) => model.id === chosenSmartModelId)?.id ?? segmentModels[0]?.id
  const interactive = useInteractiveSegment(itemId)
  const { mutateAsync: segment, reset: resetSegment } = interactive
  const onSmartPrompt = useMemo(() => {
    if (!smartModelId) return undefined
    return async (prompt: SmartPrompt): Promise<Point2D[] | null> => {
      const answer = await segment(
        prompt.kind === 'point'
          ? { model_id: smartModelId, point: { x: prompt.point[0], y: prompt.point[1] } }
          : { model_id: smartModelId, box: prompt.bbox },
      )
      return answer.points
    }
  }, [segment, smartModelId])
  useEffect(() => {
    resetSegment()
  }, [itemId, resetSegment])

  // OCR for scanned PDFs: the first `ocr` model of the organisation reads a
  // page on request; without one the annotator offers nothing.
  const ocrModelId = useMemo(
    () =>
      (modelsQuery.data?.items ?? []).find(
        (model) => model.task === 'ocr' && model.endpoint_url !== null,
      )?.id,
    [modelsQuery.data],
  )
  const { mutateAsync: readOcrPage } = useItemOcr(itemId)
  const readPdfText = useMemo(() => {
    if (!ocrModelId) return undefined
    return async (page: number) => (await readOcrPage({ model_id: ocrModelId, page })).words
  }, [ocrModelId, readOcrPage])

  useEffect(() => {
    setSelectedId(null)
    setSubmitBlocked(false)
    openedAt.current = Date.now()
  }, [itemId])

  // Queue mode (WF-2): no item in the URL means "give me the next task".
  const queueBase = `/projects/${projectId ?? ''}/annotate`
  const queue = useTaskQueue({
    projectId,
    type: 'annotate',
    queueMode: !itemId,
    onClaimed: (task) => navigate(`${queueBase}/${task.item_id}`, { replace: true }),
  })
  // Task mode: this item was reached by claiming, so the lock is ours (WF-3).
  const task = queue.task && queue.task.item_id === itemId ? queue.task : null
  const region = task?.region ?? null
  // IMG-6: the server refuses shapes anchored outside the task's region; say
  // so before the save does.
  const outOfRegion = useMemo(
    () => (region ? result.shapes.filter((shape) => !isAnchorInRegion(shape, region)) : []),
    [region, result.shapes],
  )
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

  const latestSchema = useMemo(() => {
    const versions = schemasQuery.data ?? []
    if (versions.length === 0) return undefined
    return versions.reduce((latest, current) =>
      current.version > latest.version ? current : latest,
    )
  }, [schemasQuery.data])

  const classes: LabelClass[] = useMemo(
    () => latestSchema?.definition.classes ?? [],
    [latestSchema],
  )
  const classificationFields = latestSchema?.definition.classification ?? []
  const missingRequired = useMemo(
    () => (latestSchema ? missingRequiredAttributes(result, latestSchema.definition) : []),
    [latestSchema, result],
  )

  const selectedShape = selectedId
    ? result.shapes.find((shape) => shape.id === selectedId)
    : undefined
  const selectedClass = selectedShape
    ? classes.find((cls) => cls.name === selectedShape.class)
    : undefined

  const setShapeAttribute = useCallback(
    (name: string, value: AttributeValue | undefined) => {
      if (!selectedId) return
      setResult({
        ...result,
        shapes: result.shapes.map((shape) => {
          if (shape.id !== selectedId) return shape
          const attributes = { ...shape.attributes }
          if (value === undefined) delete attributes[name]
          else attributes[name] = value
          return { ...shape, attributes }
        }),
      })
    },
    [result, selectedId, setResult],
  )

  const setClassificationValue = useCallback(
    (name: string, value: AttributeValue | undefined) => {
      const classification = { ...result.classification }
      if (value === undefined) delete classification[name]
      else classification[name] = value
      setResult({ ...result, classification })
    },
    [result, setResult],
  )

  const createAnnotation = useCreateAnnotation(itemId ?? '')

  // Measurement scale (TOOL-8): the item's own pixel spacing wins; then a
  // scale set here with the ruler this session; then the project's.
  const projectQuery = useProject(projectId)
  const updateProject = useUpdateProject(projectId ?? '')
  const [sessionScale, setSessionScale] = useState<PixelScale | null>(null)
  const [calibrationNotice, setCalibrationNotice] = useState<string | null>(null)
  const measureScale = useMemo(() => {
    const own = resolveScale(itemQuery.data?.meta, undefined)
    if (own.source === 'item') return own
    return sessionScale ?? resolveScale(undefined, projectQuery.data?.settings)
  }, [itemQuery.data?.meta, projectQuery.data?.settings, sessionScale])
  const handleCalibrate = useCallback((unitsPerPixel: number, unit: string) => {
    setSessionScale({ x: unitsPerPixel, y: unitsPerPixel, unit, source: 'session' })
    setCalibrationNotice(null)
  }, [])
  const saveScaleForProject = useCallback(() => {
    if (!sessionScale || !projectQuery.data) return
    updateProject.mutate(
      {
        settings: {
          ...projectQuery.data.settings,
          calibration: { units_per_pixel: sessionScale.x, unit: sessionScale.unit },
        },
      },
      {
        onSuccess: () => {
          setSessionScale(null)
          setCalibrationNotice(t('page.savedScale'))
        },
        onError: (error) =>
          setCalibrationNotice(errorMessage(error, t('page.saveScaleError'))),
      },
    )
  }, [projectQuery.data, sessionScale, updateProject, t])

  // Copy from the previous item (TOOL-7): the last item saved or submitted in
  // this project during this session, e.g. the previous frame of a sequence.
  const previousItemId = useRecentItemStore((state) =>
    projectId ? state.lastSavedItem[projectId] : undefined,
  )
  const rememberSavedItem = useRecentItemStore((state) => state.rememberSavedItem)
  const copySourceId = previousItemId !== itemId ? previousItemId : undefined
  const previousAnnotations = useAnnotations(copySourceId)
  const previousResult = useMemo(() => {
    const versions = previousAnnotations.data ?? []
    if (versions.length === 0) return undefined
    return versions.reduce((a, b) => (b.version > a.version ? b : a)).result
  }, [previousAnnotations.data])
  const [copyNotice, setCopyNotice] = useState<string | null>(null)
  useEffect(() => setCopyNotice(null), [itemId])

  const handleCopyPrevious = useCallback(() => {
    if (!previousResult) return
    const outcome = copyAnnotations(result, previousResult, classes)
    setResult(outcome.result)
    const skipped = outcome.skipped ? t('page.copiedSkipped', { count: outcome.skipped }) : ''
    setCopyNotice(`${t('page.copied', { count: outcome.copied })}${skipped}.`)
  }, [classes, previousResult, result, setResult, t])

  const orderedItemIds = useMemo(
    () => (itemsQuery.data?.items ?? []).map((item) => item.id),
    [itemsQuery.data],
  )
  const currentIndex = itemId ? orderedItemIds.indexOf(itemId) : -1

  // Browsing (no task held): step through the item grid's order.
  const goToOffset = useCallback(
    (offset: number) => {
      if (currentIndex === -1 || !projectId) return
      const nextIndex = currentIndex + offset
      const nextId = orderedItemIds[nextIndex]
      if (nextId) navigate(`/projects/${projectId}/annotate/${nextId}`)
    },
    [currentIndex, navigate, orderedItemIds, projectId],
  )

  const goToQueue = useCallback(() => navigate(queueBase), [navigate, queueBase])

  const handleNext = useCallback(() => {
    if (!confirmLeave()) return
    if (task) {
      // Give the lock back and let queue mode claim the next one.
      void queue.release().then(goToQueue)
    } else {
      goToOffset(1)
    }
  }, [confirmLeave, goToOffset, goToQueue, queue, task])

  const handlePrevious = useCallback(() => {
    if (!task && confirmLeave()) goToOffset(-1)
  }, [confirmLeave, goToOffset, task])

  const handleSaveDraft = useCallback(() => {
    if (!latestSchema) return
    createAnnotation.mutate(
      {
        result,
        label_schema_version_id: latestSchema.id,
        task_id: task?.id,
      },
      {
        onSuccess: () => {
          savedRef.current = { itemId, result }
          if (projectId && itemId) rememberSavedItem(projectId, itemId)
        },
      },
    )
  }, [createAnnotation, itemId, latestSchema, projectId, rememberSavedItem, result, task])

  const handleSubmit = useCallback(() => {
    if (!latestSchema) return
    if (missingRequired.length > 0) {
      // The server would refuse it (QA-6): point at the first gap instead.
      setSubmitBlocked(true)
      const shapeId = missingRequired.find((gap) => gap.shapeId !== null)?.shapeId
      if (shapeId) {
        setSelectedId(shapeId)
        setSelectRequest((last) => ({ id: shapeId, seq: (last?.seq ?? 0) + 1 }))
      }
      return
    }
    setSubmitBlocked(false)
    createAnnotation.mutate(
      {
        result,
        label_schema_version_id: latestSchema.id,
        task_id: task?.id,
        duration_ms: Math.max(0, Math.round(Date.now() - openedAt.current)),
        submit: true,
      },
      {
        onSuccess: () => {
          savedRef.current = { itemId, result }
          if (projectId && itemId) rememberSavedItem(projectId, itemId)
          if (task) {
            // The server closed the task on submit; nothing to release.
            queue.finish()
            goToQueue()
          } else {
            goToOffset(1)
          }
        },
      },
    )
  }, [
    createAnnotation,
    goToOffset,
    goToQueue,
    itemId,
    latestSchema,
    missingRequired,
    projectId,
    queue,
    rememberSavedItem,
    result,
    task,
  ])

  const [shortcutsOpen, setShortcutsOpen] = useState(false)
  // N and P also match with Shift held. On an image, P alone is the point
  // tool, so the canvas leaves only Shift+N / Shift+P to the page.
  const navPrefix = mediaType === 'image' ? 'Shift+' : ''
  useHotkeys(
    {
      n: handleNext,
      p: handlePrevious,
      'mod+enter': () => handleSubmit(),
      'shift+c': handleCopyPrevious,
      '?': () => setShortcutsOpen(true),
    },
    true,
    // The annotators own letter keys (tools, class hotkeys); see lib/hotkeys.
    { fallback: true },
  )

  const shortcutGroups: ShortcutGroup[] = [
    {
      title: t('shortcuts.item'),
      items: [
        { keys: `${navPrefix}N`, label: task ? t('shortcuts.nextTask') : t('shortcuts.next') },
        ...(task ? [] : [{ keys: `${navPrefix}P`, label: t('shortcuts.previous') }]),
        { keys: 'Ctrl/Cmd+Enter', label: t('shortcuts.submit') },
        { keys: 'Shift+C', label: t('shortcuts.copy') },
        { keys: '?', label: t('shortcuts.help') },
      ],
    },
    ...(mediaType === 'image'
      ? [
          {
            title: t('shortcuts.tools'),
            items: [
              ...TOOLS.map(({ tool, hotkey }) => ({ keys: hotkey, label: t(`tools.${tool}`) })),
              { keys: 'N', label: t('shortcuts.skipKeypoint') },
            ],
          },
          {
            title: t('shortcuts.canvas'),
            items: [
              { keys: 'Ctrl/Cmd+Z', label: t('shortcuts.undo') },
              { keys: 'Ctrl/Cmd+Shift+Z', label: t('shortcuts.redo') },
              { keys: '+ / −', label: t('shortcuts.zoom') },
              { keys: '0', label: t('shortcuts.fit') },
              { keys: 'Space', label: t('shortcuts.pan') },
              { keys: t('shortcuts.middleButton'), label: t('shortcuts.panMouse') },
              { keys: 'I', label: t('shortcuts.adjust') },
              { keys: '[ / ]', label: t('shortcuts.brushSize') },
              { keys: 'Delete / Backspace', label: t('shortcuts.delete') },
              { keys: '← ↑ → ↓', label: t('shortcuts.nudge') },
              { keys: 'Enter', label: t('shortcuts.finish') },
              { keys: 'Backspace', label: t('shortcuts.removePoint') },
              { keys: 'Esc', label: t('shortcuts.cancel') },
            ],
          },
        ]
      : []),
    {
      title: t('shortcuts.classes'),
      items: classes.flatMap((cls) =>
        cls.hotkey ? [{ keys: cls.hotkey, label: cls.display_name ?? cls.name }] : [],
      ),
    },
  ]

  if (!projectId) {
    return <ErrorState title={t('shared.missingProjectId')} />
  }

  if (!itemId) {
    return (
      <div className="flex flex-1 items-center justify-center p-8">
        {queue.error ? (
          <ErrorState
            title={t('page.claimError')}
            message={errorMessage(queue.error, t('shared.unknownError'))}
            onRetry={queue.claimNext}
          />
        ) : queue.empty ? (
          <EmptyState
            title={t('page.queueEmptyTitle')}
            message={t('page.queueEmptyMessage')}
            action={
              <div className="flex gap-2">
                <Button variant="secondary" onClick={queue.claimNext}>
                  {t('shared.checkAgain')}
                </Button>
                <Link to={`/projects/${projectId}`} className="text-sm text-accent underline">
                  {t('shared.backToProject')}
                </Link>
              </div>
            }
          />
        ) : (
          <Spinner label={t('page.claimingNext')} />
        )}
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
          message={
            itemQuery.error instanceof ApiError
              ? (itemQuery.error.detail ?? itemQuery.error.title)
              : t('shared.unknownError')
          }
          onRetry={() => void itemQuery.refetch()}
        />
      </div>
    )
  }

  if (annotationsQuery.isError) {
    // Starting empty here would let a save silently replace the item's work.
    return (
      <div className="flex flex-1 items-center justify-center p-8">
        <ErrorState
          title={t('page.annotationsLoadError')}
          message={
            annotationsQuery.error instanceof ApiError
              ? (annotationsQuery.error.detail ?? annotationsQuery.error.title)
              : t('shared.unknownError')
          }
          onRetry={() => void annotationsQuery.refetch()}
        />
      </div>
    )
  }

  const item = itemQuery.data
  const tiles = itemTiles(item.meta)

  const annotatorProps: ImageAnnotatorProps = {
    imageUrl: item.media_url ?? '',
    imageWidth: item.width ?? tiles?.width ?? 0,
    imageHeight: item.height ?? tiles?.height ?? 0,
    classes,
    value: result,
    onChange: setResult,
    onSelectionChange: setSelectedId,
    selectRequest,
    onSmartPrompt,
    measureScale,
    onCalibrate: handleCalibrate,
    region,
    tiles,
    itemId,
    signTiles,
  }

  const workNote = task?.gold
    ? t('page.goldNote')
    : task?.slot != null
      ? t('page.consensusNote')
      : region
        ? t('page.regionNote')
        : null

  return (
    <div className="flex flex-1 flex-col">
      <ShortcutsDialog
        open={shortcutsOpen}
        onClose={() => setShortcutsOpen(false)}
        groups={shortcutGroups}
      />
      <div className="flex flex-1 flex-col lg:flex-row">
        {/* Left sidebar: class list and hotkey hints */}
        <aside className="order-2 w-full shrink-0 border-line p-4 lg:order-1 lg:w-56 lg:border-r">
          <h2 className="mb-2 text-sm font-semibold text-ink">{t('page.classes')}</h2>
          {classes.length === 0 ? (
            <p className="text-sm text-muted">{t('page.noSchema')}</p>
          ) : (
            <ul className="space-y-1">
              {classes.map((labelClass) => (
                <li
                  key={labelClass.name}
                  className="flex items-center justify-between gap-2 rounded px-2 py-1 text-sm"
                >
                  <span className="flex items-center gap-2 text-ink">
                    <span
                      aria-hidden="true"
                      className="inline-block h-3 w-3 rounded-full"
                      style={{ backgroundColor: labelClass.color }}
                    />
                    {labelClass.display_name}
                  </span>
                  {labelClass.hotkey && (
                    <kbd className="rounded border border-line px-1.5 py-0.5 text-xs text-muted">
                      {labelClass.hotkey}
                    </kbd>
                  )}
                </li>
              ))}
            </ul>
          )}

          {segmentModels.length > 0 && (
            <section className="mt-6" aria-labelledby="smart-polygon-heading">
              <h2 id="smart-polygon-heading" className="mb-2 text-sm font-semibold text-ink">
                {t('page.smartPolygon')}
              </h2>
              {segmentModels.length > 1 ? (
                <label className="block text-sm text-muted">
                  <span className="sr-only">{t('page.segmentModel')}</span>
                  <select
                    className="w-full rounded border border-line bg-surface px-2 py-1 text-sm text-ink"
                    value={smartModelId}
                    onChange={(event) => setChosenSmartModelId(event.target.value)}
                  >
                    {segmentModels.map((model) => (
                      <option key={model.id} value={model.id}>
                        {model.name}
                      </option>
                    ))}
                  </select>
                </label>
              ) : (
                <p className="text-sm text-muted">{segmentModels[0].name}</p>
              )}
              <p className="mt-1 text-xs text-muted">
                <Trans
                  t={t}
                  i18nKey="page.smartPolygonHint"
                  components={{
                    kbd: <kbd className="rounded border border-line px-1.5 py-0.5 text-xs" />,
                  }}
                />
              </p>
              {interactive.isError && (
                <p role="alert" className="mt-1 text-xs text-danger">
                  {errorMessage(interactive.error, t('page.modelError'))}
                </p>
              )}
            </section>
          )}

          <h2 className="mb-2 mt-6 text-sm font-semibold text-ink">{t('page.hotkeys')}</h2>
          <ul className="space-y-1 text-sm text-muted">
            <li>
              <kbd className="rounded border border-line px-1.5 py-0.5 text-xs">{navPrefix}N</kbd>{' '}
              {task ? t('page.hotkeyNextTask') : t('page.hotkeyNextItem')}
            </li>
            {!task && (
              <li>
                <kbd className="rounded border border-line px-1.5 py-0.5 text-xs">{navPrefix}P</kbd>{' '}
                {t('page.hotkeyPreviousItem')}
              </li>
            )}
            <li>
              <kbd className="rounded border border-line px-1.5 py-0.5 text-xs">Ctrl/Cmd+Enter</kbd>{' '}
              {t('page.hotkeySubmit')}
            </li>
            <li>
              <kbd className="rounded border border-line px-1.5 py-0.5 text-xs">Shift+C</kbd>{' '}
              {t('page.hotkeyCopy')}
            </li>
          </ul>
          <button
            type="button"
            onClick={() => setShortcutsOpen(true)}
            className="mt-2 text-sm text-accent underline"
          >
            {t('shortcuts.open')}{' '}
            <kbd className="rounded border border-line px-1.5 py-0.5 text-xs no-underline">?</kbd>
          </button>
        </aside>

        {/* Centre pane: the annotator canvas */}
        <section className="order-1 flex flex-1 items-center justify-center bg-black/20 p-4 lg:order-2">
          {!item.media_url ? (
            <NoPreview meta={item.meta} fallback={t('page.noPreview')} />
          ) : item.media_type === 'text' ? (
            textQuery.isError ? (
              <ErrorState
                title={t('page.textLoadError')}
                message={errorMessage(textQuery.error, t('page.storageError'))}
                onRetry={() => void textQuery.refetch()}
              />
            ) : textQuery.data === undefined ? (
              <Spinner label={t('shared.loadingText')} />
            ) : (
              <Suspense fallback={<Spinner label={t('shared.loadingAnnotator')} />}>
                <TextAnnotator
                  key={itemId}
                  text={textQuery.data}
                  classes={classes}
                  value={result}
                  onChange={setResult}
                  onSelectionChange={setSelectedId}
                  pages={pdfTextOf(item.meta)?.pages}
                  onPageFocus={(page) => setPdfFocus({ page, itemId: item.id })}
                />
              </Suspense>
            )
          ) : item.media_type === 'audio' ? (
            <Suspense fallback={<Spinner label={t('shared.loadingAnnotator')} />}>
              <AudioAnnotator
                key={itemId}
                mediaUrl={item.media_url}
                sizeBytes={item.size_bytes}
                classes={classes}
                value={result}
                onChange={setResult}
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
                  key={itemId}
                  series={seriesResult.series}
                  classes={classes}
                  value={result}
                  onChange={setResult}
                  onSelectionChange={setSelectedId}
                />
              </Suspense>
            )
          ) : item.media_type === 'llm' ? (
            textQuery.isError ? (
              <ErrorState
                title={t('page.textLoadError')}
                message={errorMessage(textQuery.error, t('page.storageError'))}
                onRetry={() => void textQuery.refetch()}
              />
            ) : textQuery.data === undefined ? (
              <Spinner label={t('shared.loadingText')} />
            ) : llmDocument === null ? (
              <ErrorState title={t('page.llmDocumentError')} message={t('page.llmDocumentHint')} />
            ) : (
              <Suspense fallback={<Spinner label={t('shared.loadingAnnotator')} />}>
                <LlmAnnotator
                  key={itemId}
                  document={llmDocument}
                  classes={classes}
                  value={result}
                  onChange={setResult}
                  onSelectionChange={setSelectedId}
                />
              </Suspense>
            )
          ) : item.media_type === 'image' ? (
            <Suspense fallback={<Spinner label={t('shared.loadingAnnotator')} />}>
              <ImageAnnotator key={itemId} {...annotatorProps} />
            </Suspense>
          ) : item.media_type === 'pdf' ? (
            <Suspense fallback={<Spinner label={t('shared.loadingAnnotator')} />}>
              <PdfAnnotator
                key={itemId}
                pdfUrl={item.media_url}
                classes={classes}
                value={result}
                onChange={setResult}
                onSelectionChange={setSelectedId}
                readText={readPdfText}
              />
            </Suspense>
          ) : item.media_type === 'video' ? (
            <Suspense fallback={<Spinner label={t('shared.loadingAnnotator')} />}>
              <VideoAnnotator
                key={itemId}
                videoUrl={item.media_url}
                width={item.width ?? 0}
                height={item.height ?? 0}
                fps={typeof item.meta.fps === 'number' ? item.meta.fps : 25}
                classes={classes}
                value={result}
                onChange={setResult}
                onSelectionChange={setSelectedId}
              />
            </Suspense>
          ) : (
            <p className="text-sm text-muted">
              {t('page.unsupportedMedia', { mediaType: item.media_type })}
            </p>
          )}
        </section>

        {/* Right sidebar: annotide list and item metadata */}
        <aside className="order-3 w-full shrink-0 border-line p-4 lg:w-72 lg:border-l">
          {workNote && (
            <p
              data-testid="work-note"
              className="mb-4 rounded border border-accent/50 px-2 py-1 text-xs text-ink"
            >
              {workNote}
            </p>
          )}
          {outOfRegion.length > 0 && (
            <p role="alert" className="mb-4 text-xs text-danger">
              {t('page.outOfRegion', { count: outOfRegion.length })}
            </p>
          )}
          <h2 className="mb-2 text-sm font-semibold text-ink">{t('shared.item')}</h2>
          <dl className="mb-6 space-y-1 text-sm">
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
            <div className="flex justify-between gap-2">
              <dt className="text-muted">{t('page.size')}</dt>
              <dd className="text-ink">
                {item.width ?? '?'} × {item.height ?? '?'}
              </dd>
            </div>
            {task && (
              <div className="flex items-center justify-between gap-2">
                <dt className="text-muted">{t('page.task')}</dt>
                <dd className="flex items-center gap-2 text-ink">
                  <span title={task.locked_until ?? undefined}>
                    {t('page.lockedUntil', {
                      time: task.locked_until
                        ? new Date(task.locked_until).toLocaleTimeString()
                        : '?',
                    })}
                  </span>
                  {task.deadline && (
                    <span
                      className={
                        new Date(task.deadline).getTime() < Date.now() ? 'text-danger' : undefined
                      }
                      title={task.deadline}
                    >
                      {t('page.due', { date: new Date(task.deadline).toLocaleDateString() })}
                    </span>
                  )}
                  <Button variant="ghost" size="sm" onClick={handleNext}>
                    {t('shared.release')}
                  </Button>
                </dd>
              </div>
            )}
          </dl>

          <ViewsPanel
            itemId={item.id}
            meta={item.meta}
            pdfFocus={pdfFocus?.itemId === item.id ? pdfFocus : undefined}
          />

          {classificationFields.length > 0 && (
            <section aria-labelledby="classification-heading" className="mb-6">
              <h2 id="classification-heading" className="mb-2 text-sm font-semibold text-ink">
                {t('shared.classification')}
              </h2>
              <AttributeEditor
                idPrefix="classification"
                fields={classificationFields}
                values={result.classification}
                onChange={setClassificationValue}
              />
            </section>
          )}

          <section aria-labelledby="selected-shape-heading" className="mb-6">
            <h2 id="selected-shape-heading" className="mb-2 text-sm font-semibold text-ink">
              {t('shared.selectedShape')}
            </h2>
            {!selectedShape ? (
              <p className="text-sm text-muted">{t('page.selectShapeHint')}</p>
            ) : (
              <>
                <p className="mb-2 text-sm text-ink">
                  {selectedClass?.display_name ?? selectedShape.class}{' '}
                  <span className="text-xs text-muted">{selectedShape.type}</span>
                </p>
                <AttributeEditor
                  idPrefix="shape"
                  fields={selectedClass?.attributes ?? []}
                  values={selectedShape.attributes}
                  onChange={setShapeAttribute}
                />
              </>
            )}
          </section>

          <div className="mb-2 flex items-center justify-between gap-2">
            <h2 id="shape-list-heading" className="text-sm font-semibold text-ink">
              {t('page.annotationsCount', { count: result.shapes.length })}
            </h2>
            <Button
              size="sm"
              variant="secondary"
              disabled={!previousResult || previousResult.shapes.length === 0}
              title={
                previousResult ? t('page.copyTitleAvailable') : t('page.copyTitleUnavailable')
              }
              onClick={handleCopyPrevious}
            >
              {t('page.copyFromPrevious')}
            </Button>
          </div>
          {copyNotice && (
            <p role="status" className="mb-2 text-xs text-muted">
              {copyNotice}
            </p>
          )}
          {result.shapes.length === 0 ? (
            <p className="text-sm text-muted">{t('page.noShapesYet')}</p>
          ) : (
            <ul aria-labelledby="shape-list-heading" className="space-y-1">
              {result.shapes.map((shape) => (
                <li key={shape.id}>
                  {/* A button, so a shape can be selected without a pointer (UX-7). */}
                  <button
                    type="button"
                    aria-pressed={shape.id === selectedId}
                    onClick={() => {
                      const id = shape.id === selectedId ? null : shape.id
                      setSelectedId(id)
                      setSelectRequest((last) => ({ id, seq: (last?.seq ?? 0) + 1 }))
                    }}
                    className={`flex w-full items-center justify-between rounded px-2 py-1 text-left
                      text-sm text-ink hover:bg-line/30 ${shape.id === selectedId ? 'bg-line/40' : ''}`}
                  >
                    <span>{shape.class}</span>
                    <span className="text-right text-xs text-muted">
                      {describeShape(shape, measureScale) ?? shape.type}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}

          <h2 className="mb-2 mt-6 text-sm font-semibold text-ink">{t('page.scale')}</h2>
          <p className="text-sm text-ink">
            {measureScale.source === 'pixels'
              ? t('page.scalePixels')
              : t('page.scalePerPixel', {
                  value: measureScale.x.toPrecision(4),
                  unit: measureScale.unit,
                }) +
                ({
                  item: t('page.scaleSourceItem'),
                  project: t('page.scaleSourceProject'),
                  session: t('page.scaleSourceSession'),
                }[measureScale.source] ?? '')}
          </p>
          {measureScale.source === 'session' && (
            <Button
              size="sm"
              variant="secondary"
              className="mt-2"
              disabled={updateProject.isPending}
              onClick={saveScaleForProject}
            >
              {t('page.saveScale')}
            </Button>
          )}
          {calibrationNotice && (
            <p role="status" className="mt-1 text-xs text-muted">
              {calibrationNotice}
            </p>
          )}

          <h2 className="mb-2 mt-6 text-sm font-semibold text-ink">{t('shared.comments')}</h2>
          <CommentsPanel itemId={itemId} />
        </aside>
      </div>

      {/* Bottom bar: Skip / Save draft / Submit */}
      <div
        className="flex items-center justify-end gap-2 border-t border-line px-4 py-3"
        aria-live="polite"
      >
        {submitBlocked && missingRequired.length > 0 ? (
          <span className="mr-auto text-sm text-danger" role="alert">
            {t('page.missingRequired', {
              count: missingRequired.length,
              names: [...new Set(missingRequired.map((gap) => gap.attribute))].join(', '),
            })}
          </span>
        ) : (
          createAnnotation.isError && (
            <span className="mr-auto text-sm text-danger">
              {errorMessage(createAnnotation.error, t('page.saveAnnotationError'))}
            </span>
          )
        )}
        <Button variant="secondary" onClick={handleNext}>
          {t('page.skip')}
        </Button>
        <Button
          variant="secondary"
          onClick={handleSaveDraft}
          disabled={createAnnotation.isPending || !latestSchema}
        >
          {createAnnotation.isPending ? t('common:saving') : t('page.saveDraft')}
        </Button>
        <Button
          variant="primary"
          onClick={handleSubmit}
          disabled={createAnnotation.isPending || !latestSchema}
        >
          {createAnnotation.isPending ? t('page.submitting') : t('page.submit')}
        </Button>
      </div>
    </div>
  )
}
