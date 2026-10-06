/**
 * PDF annotator (TOOL, CONTRACTS.md "PDF items"): pdf.js draws one page into
 * a canvas, and an SVG overlay in that page's own point space holds the
 * shapes of the page. Boxes are drawn by dragging; each new box records the
 * words of the page's text layer inside it as `text`, so document fields
 * (an invoice total, a signature block) come with their content.
 *
 * Drawn shapes are edited like on images (TOOL): drag a shape to move it,
 * drag the handles of the selected one to resize it or move a vertex,
 * Alt+click a vertex to remove it. The shared `annotator/edit` functions do
 * the geometry in page points; a box's `text` is re-read when it lands.
 *
 * A scanned page has no text layer. When the page offers `readText` (an
 * `ocr` model of the organisation), a button reads the page through it; the
 * words then serve exactly as a text layer would, boxes already drawn on the
 * page get theirs, and they are kept for the session, never stored.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { MouseEvent as ReactMouseEvent } from 'react'
import { useTranslation } from 'react-i18next'

import type {
  AnnotationResult,
  BBox,
  BBoxShape,
  LabelClass,
  PdfSpanShape,
  RelationShape,
  Shape,
} from '@/api/types'
import { Spinner } from '@/components/Spinner'
import { useHotkeys } from '@/lib/hotkeys'
import { newShapeId } from '@/lib/ids'
import { bboxFromCorners, clientToImagePoint } from '@/features/video-annotator/pointer'
import {
  dragHandle,
  isMovable,
  removeVertex,
  shapeHandles,
  translateShape,
} from '@/features/annotator/edit'
import { isRelation, removeShape } from '@/features/text-annotator/offsets'
import { openPdf } from './pdf'
import type { PdfDocument, PdfPage } from './pdf'
import { arrowHead, shapeAnchor } from './relations'
import { RelationList } from './RelationList'
import { spanGeometry, textInBox, wordAt, wordRun, lineBoxes } from './words'
import type { Word } from './words'

export interface PdfAnnotatorProps {
  pdfUrl: string
  classes: LabelClass[]
  value: AnnotationResult
  onChange: (next: AnnotationResult) => void
  readOnly?: boolean
  onSelectionChange?: (id: string | null) => void
  /** Injected in tests; defaults to pdf.js. */
  open?: (url: string) => Promise<PdfDocument>
  /** Reads a page's words by OCR (1-based page); offered where the text layer is empty. */
  readText?: (page: number) => Promise<Word[]>
}

/** Device pixels per PDF point: sharp on retina without huge canvases. */
const RENDER_SCALE = Math.min(
  3,
  2 * (typeof window === 'undefined' ? 1 : window.devicePixelRatio || 1),
)
/** Smaller drags are clicks, not boxes (in points). */
const MIN_BOX = 2
/** Zoom steps; 1 fits the page to the pane (up to 1.5 px per point). */
const ZOOMS = [0.5, 0.75, 1, 1.5, 2, 3] as const
/** A polygon closes when the first vertex is clicked within this many screen px. */
const CLOSE_PX = 8

type PdfTool = 'bbox' | 'polygon' | 'point' | 'span'
const TOOLS: PdfTool[] = ['bbox', 'polygon', 'point', 'span']
/** Side of an edit handle, in screen px. */
const HANDLE_PX = 8

/** A move or handle drag in progress; `preview` is what a release commits. */
interface EditGesture {
  /** Handle index, or null when the whole shape moves. */
  handle: number | null
  start: [number, number]
  original: Shape
  preview: Shape
}

function colorFor(classes: LabelClass[], className: string): string {
  return classes.find((c) => c.name === className)?.color ?? '#94a3b8'
}

function clamp(box: BBox, width: number, height: number): BBox {
  return [
    Math.max(0, Math.min(box[0], width)),
    Math.max(0, Math.min(box[1], height)),
    Math.max(0, Math.min(box[2], width)),
    Math.max(0, Math.min(box[3], height)),
  ]
}

export function PdfAnnotator({
  pdfUrl,
  classes,
  value,
  onChange,
  readOnly = false,
  onSelectionChange,
  open = openPdf,
  readText,
}: PdfAnnotatorProps): JSX.Element {
  const { t } = useTranslation('annotator')
  const [doc, setDoc] = useState<PdfDocument | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [pageNumber, setPageNumber] = useState(1)
  const [page, setPage] = useState<PdfPage | null>(null)
  const [textLayer, setTextLayer] = useState<Word[] | null>(null)
  /** Words read by OCR, per page, for this session. */
  const [ocrWords, setOcrWords] = useState<ReadonlyMap<number, Word[]>>(new Map())
  const [ocrStatus, setOcrStatus] = useState<{ reading: boolean; error: string | null }>({
    reading: false,
    error: null,
  })
  const ocrPageWords = ocrWords.get(pageNumber)
  const words = useMemo(
    () => (textLayer && textLayer.length > 0 ? textLayer : (ocrPageWords ?? [])),
    [ocrPageWords, textLayer],
  )
  /** The latest value, for an OCR answer that arrives after further edits. */
  const valueRef = useRef(value)
  useEffect(() => {
    valueRef.current = value
  }, [value])
  /** The page on screen, so a late OCR answer does not report on another page. */
  const pageRef = useRef(pageNumber)
  useEffect(() => {
    pageRef.current = pageNumber
  }, [pageNumber])
  const [drag, setDrag] = useState<{
    start: [number, number]
    current: [number, number]
  } | null>(null)
  const [selectedId, setSelectedIdState] = useState<string | null>(null)
  const [activeClassName, setActiveClassName] = useState<string | null>(null)
  const [chosenTool, setTool] = useState<PdfTool>('bbox')
  /** A span being selected: word indices into `words`, and the shape pressed on, if any. */
  const [spanSel, setSpanSel] = useState<{
    start: number
    end: number
    onShape: Shape | null
  } | null>(null)
  const [activeRelation, setActiveRelation] = useState<string | null>(null)
  /** Source shape of a relation being linked; its target is the next shape clicked. */
  const [linkFrom, setLinkFrom] = useState<string | null>(null)
  const [zoomIndex, setZoomIndex] = useState(ZOOMS.indexOf(1))
  const zoom = ZOOMS[zoomIndex]
  /** Vertices of the polygon being drawn, and where the pointer is. */
  const [draft, setDraft] = useState<Array<[number, number]>>([])
  const [hover, setHover] = useState<[number, number] | null>(null)
  const [edit, setEdit] = useState<EditGesture | null>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const svgRef = useRef<SVGSVGElement>(null)

  const classesFor = useCallback(
    (kind: string) => classes.filter((c) => (c.tools as string[]).includes(kind)),
    [classes],
  )
  const availableTools = useMemo(
    () => TOOLS.filter((candidate) => classesFor(candidate).length > 0),
    [classesFor],
  )
  const tool = availableTools.includes(chosenTool) ? chosenTool : (availableTools[0] ?? chosenTool)
  const toolClasses = useMemo(() => classesFor(tool), [classesFor, tool])
  const relationClasses = useMemo(() => classesFor('relation'), [classesFor])
  const currentRelation =
    relationClasses.find((c) => c.name === activeRelation)?.name ?? relationClasses[0]?.name ?? null
  const currentClass =
    toolClasses.find((c) => c.name === activeClassName)?.name ?? toolClasses[0]?.name ?? null

  const select = useCallback(
    (id: string | null) => {
      setSelectedIdState(id)
      onSelectionChange?.(id)
    },
    [onSelectionChange],
  )

  // Open the document once per URL; the worker is torn down with it.
  useEffect(() => {
    let cancelled = false
    let opened: PdfDocument | null = null
    setDoc(null)
    setLoadError(null)
    open(pdfUrl)
      .then((loaded) => {
        opened = loaded
        if (cancelled) void loaded.destroy()
        else setDoc(loaded)
      })
      .catch((error: unknown) => {
        if (!cancelled) setLoadError(error instanceof Error ? error.message : String(error))
      })
    return () => {
      cancelled = true
      if (opened) void opened.destroy()
    }
  }, [open, pdfUrl])

  // Fetch the current page, its words, and draw it.
  useEffect(() => {
    if (!doc) return
    let cancelled = false
    setPage(null)
    setTextLayer(null)
    setOcrStatus({ reading: false, error: null })
    void doc.page(pageNumber).then(async (loaded) => {
      if (cancelled) return
      setPage(loaded)
      try {
        const pageWords = await loaded.words()
        if (!cancelled) setTextLayer(pageWords)
      } catch {
        // No text layer (a scan): boxes carry no text unless the page is OCR'd.
        if (!cancelled) setTextLayer([])
      }
    })
    return () => {
      cancelled = true
    }
  }, [doc, pageNumber])

  // Drawn again at each zoom so text stays sharp; capped to bound memory.
  useEffect(() => {
    if (page && canvasRef.current) {
      void page.render(canvasRef.current, Math.min(8, RENDER_SCALE * zoom))
    }
  }, [page, zoom])

  const pageShapes = useMemo(
    () => value.shapes.filter((shape) => !isRelation(shape) && (shape.page ?? 1) === pageNumber),
    [pageNumber, value.shapes],
  )
  const selected = value.shapes.find((shape) => shape.id === selectedId) ?? null
  const relations = useMemo(() => value.shapes.filter(isRelation), [value.shapes])
  const shapeById = useMemo(() => new Map(value.shapes.map((s) => [s.id, s])), [value.shapes])
  /** Words are what a span is made of; a page without any cannot take one. */
  const spanBlocked = tool === 'span' && textLayer !== null && words.length === 0

  const setShapes = useCallback(
    (shapes: Shape[]) => onChange({ ...value, media_type: 'pdf', shapes }),
    [onChange, value],
  )

  const toPagePoint = useCallback(
    (event: ReactMouseEvent): [number, number] => {
      const width = page?.width ?? 1
      const height = page?.height ?? 1
      const rect = svgRef.current?.getBoundingClientRect() ?? {
        left: 0,
        top: 0,
        width,
        height,
      }
      return clientToImagePoint(rect, width, height, event.clientX, event.clientY)
    },
    [page],
  )

  const addShape = useCallback(
    (shape: Shape) => {
      setShapes([...value.shapes, shape])
      select(shape.id)
    },
    [select, setShapes, value.shapes],
  )

  const finishPolygon = useCallback(() => {
    if (draft.length >= 3 && currentClass) {
      addShape({
        id: newShapeId(),
        type: 'polygon',
        class: currentClass,
        attributes: {},
        confidence: null,
        page: pageNumber,
        points: draft,
      })
    }
    setDraft([])
  }, [addShape, currentClass, draft, pageNumber])

  /** Page points per screen pixel, for hit radii that feel the same at any zoom. */
  const pointsPerPixel = (): number => {
    const rect = svgRef.current?.getBoundingClientRect()
    return rect && rect.width > 0 && page ? page.width / rect.width : 1
  }

  const addRelation = (from: string, to: string): void => {
    if (!currentRelation || from === to) return
    const relation: RelationShape = {
      id: newShapeId(),
      type: 'relation',
      class: currentRelation,
      attributes: {},
      confidence: null,
      from,
      to,
    }
    addShape(relation)
  }

  const startLink = (): void => {
    if (readOnly || !selected || isRelation(selected) || !currentRelation) return
    setLinkFrom(selected.id)
  }

  const handleBackgroundMouseDown = (event: ReactMouseEvent): void => {
    if (event.target !== svgRef.current) return
    if (linkFrom) return
    select(null)
    if (readOnly || !currentClass || !page) return
    const point = toPagePoint(event)
    if (tool === 'span') {
      const index = wordAt(words, point)
      if (index >= 0) setSpanSel({ start: index, end: index, onShape: null })
    } else if (tool === 'bbox') {
      setDrag({ start: point, current: point })
    } else if (tool === 'point') {
      addShape({
        id: newShapeId(),
        type: 'point',
        class: currentClass,
        attributes: {},
        confidence: null,
        page: pageNumber,
        point,
      })
    } else {
      const first = draft[0]
      const radius = CLOSE_PX * pointsPerPixel()
      if (
        first &&
        draft.length >= 3 &&
        Math.hypot(point[0] - first[0], point[1] - first[1]) <= radius
      ) {
        finishPolygon()
      } else {
        setDraft([...draft, point])
      }
    }
  }

  /** A box's words follow it: re-read them wherever it ends up. */
  const withText = (shape: Shape): Shape => {
    if (shape.type !== 'bbox') return shape
    const rest: BBoxShape = { ...shape }
    delete rest.text
    const text = textInBox(words, shape.bbox)
    return text !== undefined ? { ...rest, text } : rest
  }

  const startEdit = (event: ReactMouseEvent, shape: Shape, handle: number | null): void => {
    event.stopPropagation()
    if (linkFrom && handle === null) {
      addRelation(linkFrom, shape.id)
      setLinkFrom(null)
      return
    }
    if (!readOnly && tool === 'span' && currentClass && handle === null) {
      // A press on a word starts a span even over another shape; releasing on
      // that same word is a click that selects the shape instead.
      const index = wordAt(words, toPagePoint(event))
      if (index >= 0) {
        setSpanSel({ start: index, end: index, onShape: shape })
        return
      }
    }
    select(shape.id)
    if (readOnly || (handle === null && !isMovable(shape))) return
    if (handle !== null && event.altKey) {
      const next = removeVertex(shape, handle)
      if (next) setShapes(value.shapes.map((s) => (s.id === shape.id ? next : s)))
      return
    }
    setEdit({ handle, start: toPagePoint(event), original: shape, preview: shape })
  }

  const handleMouseMove = (event: ReactMouseEvent): void => {
    if (edit && page) {
      const point = toPagePoint(event)
      const next =
        edit.handle === null
          ? translateShape(
              edit.original,
              point[0] - edit.start[0],
              point[1] - edit.start[1],
              page.width,
              page.height,
            )
          : dragHandle(edit.original, edit.handle, point, page.width, page.height)
      // Null is a degenerate result: keep the last good preview.
      if (next) setEdit({ ...edit, preview: next })
    } else if (spanSel) {
      const end = wordAt(words, toPagePoint(event), true)
      if (end >= 0 && end !== spanSel.end) setSpanSel({ ...spanSel, end })
    } else if (drag) setDrag({ ...drag, current: toPagePoint(event) })
    else if (draft.length > 0) setHover(toPagePoint(event))
  }

  const handleMouseUp = (): void => {
    if (edit) {
      setEdit(null)
      if (edit.preview !== edit.original) {
        const next = withText(edit.preview)
        setShapes(value.shapes.map((s) => (s.id === next.id ? next : s)))
      }
      return
    }
    if (spanSel) {
      const { start, end, onShape } = spanSel
      setSpanSel(null)
      if (onShape && start === end) {
        select(onShape.id)
        return
      }
      const geometry = currentClass ? spanGeometry(words, start, end) : null
      if (!geometry || !currentClass) return
      const span: PdfSpanShape = {
        id: newShapeId(),
        type: 'span',
        class: currentClass,
        attributes: {},
        confidence: null,
        page: pageNumber,
        boxes: geometry.boxes,
        text: geometry.text,
      }
      addShape(span)
      return
    }
    if (!drag || !page || !currentClass) {
      setDrag(null)
      return
    }
    const box = clamp(bboxFromCorners(drag.start, drag.current), page.width, page.height)
    setDrag(null)
    if (box[2] - box[0] < MIN_BOX || box[3] - box[1] < MIN_BOX) return
    const text = textInBox(words, box)
    const shape: BBoxShape = {
      id: newShapeId(),
      type: 'bbox',
      class: currentClass,
      attributes: {},
      confidence: null,
      page: pageNumber,
      bbox: box,
      ...(text !== undefined ? { text } : {}),
    }
    addShape(shape)
  }

  const readPageText = async (): Promise<void> => {
    if (!readText) return
    const target = pageNumber
    setOcrStatus({ reading: true, error: null })
    let found: Word[]
    try {
      found = await readText(target)
    } catch (error) {
      if (pageRef.current === target) {
        setOcrStatus({
          reading: false,
          error: error instanceof Error ? error.message : String(error),
        })
      }
      return
    }
    if (pageRef.current === target) setOcrStatus({ reading: false, error: null })
    // Kept even if the person moved on: the words are there when they come back.
    setOcrWords((previous) => new Map(previous).set(target, found))
    // Boxes drawn on the page before it was read get their words now.
    const current = valueRef.current
    let changed = false
    const shapes = current.shapes.map((shape) => {
      if (shape.type !== 'bbox' || shape.text || (shape.page ?? 1) !== target) return shape
      const text = textInBox(found, shape.bbox)
      if (text === undefined) return shape
      changed = true
      return { ...shape, text }
    })
    if (changed) onChange({ ...current, media_type: 'pdf', shapes })
  }

  const deleteSelected = useCallback(() => {
    if (readOnly || !selectedId) return
    setShapes(removeShape(value.shapes, selectedId))
    select(null)
  }, [readOnly, select, selectedId, setShapes, value.shapes])

  const reclassSelected = (className: string): void => {
    if (!selected) return
    setShapes(
      value.shapes.map((shape) =>
        shape.id === selected.id ? { ...shape, class: className } : shape,
      ),
    )
  }

  const labelOf = (name: string): string =>
    classes.find((c) => c.name === name)?.display_name || name
  /** A shape for lists and prompts: its class, text and page. */
  const describe = (id: string): string => {
    const shape = shapeById.get(id)
    if (!shape) return id
    const text = shape.type === 'bbox' || shape.type === 'span' ? shape.text : null
    const where = t('pdf.relation.onPage', { page: shape.page ?? 1 })
    return `${labelOf(shape.class)}${text ? ` “${text}”` : ''} (${where})`
  }

  const pageCount = doc?.pageCount ?? 0
  const goToPage = useCallback(
    (next: number) => {
      if (pageCount === 0) return
      setPageNumber(Math.min(Math.max(1, next), pageCount))
      setDraft([])
      setEdit(null)
      setSpanSel(null)
      select(null)
    },
    [pageCount, select],
  )

  useHotkeys(
    {
      pageup: () => goToPage(pageNumber - 1),
      pagedown: () => goToPage(pageNumber + 1),
      delete: deleteSelected,
      backspace: deleteSelected,
      enter: finishPolygon,
      escape: () => {
        setDraft([])
        setLinkFrom(null)
        setSpanSel(null)
      },
      r: startLink,
      '+': () => setZoomIndex((i) => Math.min(ZOOMS.length - 1, i + 1)),
      '=': () => setZoomIndex((i) => Math.min(ZOOMS.length - 1, i + 1)),
      '-': () => setZoomIndex((i) => Math.max(0, i - 1)),
      '0': () => setZoomIndex(ZOOMS.indexOf(1)),
    },
    true,
  )

  if (loadError) {
    return (
      <p role="alert" className="max-w-prose text-sm text-danger">
        {t('pdf.loadError', { message: loadError })}
      </p>
    )
  }
  if (!doc) return <Spinner label={t('pdf.loading')} />

  return (
    <div className="flex w-full flex-col items-center gap-2">
      <div className="flex flex-wrap items-center gap-2 text-sm text-ink">
        <button
          type="button"
          className="rounded border border-line px-2 py-1 disabled:opacity-40"
          aria-label={t('pdf.previous')}
          disabled={pageNumber <= 1}
          onClick={() => goToPage(pageNumber - 1)}
        >
          ◀
        </button>
        <span data-testid="page-counter" className="text-muted">
          {t('pdf.page', { page: pageNumber, count: pageCount })}
        </span>
        <button
          type="button"
          className="rounded border border-line px-2 py-1 disabled:opacity-40"
          aria-label={t('pdf.next')}
          disabled={pageNumber >= pageCount}
          onClick={() => goToPage(pageNumber + 1)}
        >
          ▶
        </button>
        <span className="ml-4 flex items-center gap-1">
          <button
            type="button"
            className="rounded border border-line px-2 py-1 disabled:opacity-40"
            aria-label={t('pdf.zoomOut')}
            disabled={zoomIndex === 0}
            onClick={() => setZoomIndex(zoomIndex - 1)}
          >
            −
          </button>
          <button
            type="button"
            className="min-w-[3.5rem] rounded border border-line px-2 py-1"
            aria-label={t('pdf.zoomReset')}
            data-testid="pdf-zoom"
            onClick={() => setZoomIndex(ZOOMS.indexOf(1))}
          >
            {Math.round(zoom * 100)} %
          </button>
          <button
            type="button"
            className="rounded border border-line px-2 py-1 disabled:opacity-40"
            aria-label={t('pdf.zoomIn')}
            disabled={zoomIndex === ZOOMS.length - 1}
            onClick={() => setZoomIndex(zoomIndex + 1)}
          >
            +
          </button>
        </span>
        {!readOnly && availableTools.length > 1 && (
          <span role="group" aria-label={t('pdf.tool')} className="ml-4 flex items-center gap-1">
            {availableTools.map((candidate) => (
              <button
                key={candidate}
                type="button"
                aria-pressed={tool === candidate}
                className={`rounded border px-2 py-1 ${
                  tool === candidate ? 'border-accent text-ink' : 'border-line text-muted'
                }`}
                onClick={() => {
                  setTool(candidate)
                  setDraft([])
                  setSpanSel(null)
                }}
              >
                {t(`pdf.tools.${candidate}`)}
              </button>
            ))}
          </span>
        )}
        {!readOnly && toolClasses.length > 0 && (
          <label className="ml-4 flex items-center gap-1">
            <span className="text-muted">{t('pdf.class')}</span>
            <select
              className="rounded border border-line bg-surface px-1 py-0.5 text-ink"
              value={currentClass ?? ''}
              onChange={(event) => setActiveClassName(event.target.value)}
            >
              {toolClasses.map((c) => (
                <option key={c.name} value={c.name}>
                  {c.display_name}
                </option>
              ))}
            </select>
          </label>
        )}
        {!readOnly && relationClasses.length > 0 && (
          <span className="ml-4 flex items-center gap-1">
            <label className="flex items-center gap-1">
              <span className="text-muted">{t('pdf.relation.label')}</span>
              <select
                aria-label={t('pdf.relation.class')}
                className="rounded border border-line bg-surface px-1 py-0.5 text-ink"
                value={currentRelation ?? ''}
                onChange={(event) => setActiveRelation(event.target.value)}
              >
                {relationClasses.map((c) => (
                  <option key={c.name} value={c.name}>
                    {c.display_name}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className="rounded border border-line px-2 py-1 disabled:opacity-40"
              disabled={!selected || isRelation(selected)}
              title={t('pdf.relation.linkTitle')}
              onClick={startLink}
            >
              {t('pdf.relation.link')}
            </button>
          </span>
        )}
        {!readOnly && readText && page && textLayer?.length === 0 && (
          <span className="ml-4 flex items-center gap-2">
            {ocrPageWords ? (
              <span className="text-muted" data-testid="pdf-ocr-done">
                {t('pdf.ocr.done', { count: ocrPageWords.length })}
              </span>
            ) : (
              <button
                type="button"
                className="rounded border border-accent px-2 py-1 disabled:opacity-40"
                title={t('pdf.ocr.hint')}
                disabled={ocrStatus.reading}
                onClick={() => void readPageText()}
              >
                {ocrStatus.reading ? t('pdf.ocr.reading') : t('pdf.ocr.read')}
              </button>
            )}
            {ocrStatus.error && (
              <span role="alert" className="text-danger">
                {t('pdf.ocr.error', { message: ocrStatus.error })}
              </span>
            )}
          </span>
        )}
        {draft.length >= 3 && (
          <button
            type="button"
            className="rounded border border-accent px-2 py-1"
            onClick={finishPolygon}
          >
            {t('pdf.finishPolygon')}
          </button>
        )}
      </div>

      {linkFrom && (
        <p role="status" className="text-sm text-accent">
          {t('pdf.relation.linking', { shape: describe(linkFrom) })}
        </p>
      )}
      {!readOnly && spanBlocked && page && (
        <p role="status" className="text-sm text-muted" data-testid="pdf-span-blocked">
          {t('pdf.span.noWords')}
        </p>
      )}

      {page ? (
        <div className="max-h-[80vh] w-full overflow-auto">
          <div
            className="relative mx-auto bg-white shadow"
            style={{
              // At 100 % the page fits the pane; zooming in scrolls it.
              width: page.width * 1.5 * zoom,
              maxWidth: zoom <= 1 ? '100%' : undefined,
              aspectRatio: `${page.width} / ${page.height}`,
            }}
          >
            <canvas
              ref={canvasRef}
              className="absolute inset-0 h-full w-full"
              data-testid="pdf-canvas"
            />
            <svg
              ref={svgRef}
              viewBox={`0 0 ${page.width} ${page.height}`}
              className="absolute inset-0 h-full w-full"
              data-testid="pdf-overlay"
              onMouseDown={handleBackgroundMouseDown}
              onMouseMove={handleMouseMove}
              onMouseUp={handleMouseUp}
              onMouseLeave={handleMouseUp}
            >
              {pageShapes.map((stored) => {
                const shape = edit?.original.id === stored.id ? edit.preview : stored
                const color = colorFor(classes, shape.class)
                const isSelected = shape.id === selectedId
                const onMouseDown = (event: ReactMouseEvent): void =>
                  startEdit(event, stored, null)
                if (shape.type === 'bbox') {
                  const [x1, y1, x2, y2] = shape.bbox
                  return (
                    <rect
                      key={shape.id}
                      data-testid={`pdf-box-${shape.id}`}
                      x={x1}
                      y={y1}
                      width={x2 - x1}
                      height={y2 - y1}
                      fill={color}
                      fillOpacity={isSelected ? 0.25 : 0.1}
                      stroke={color}
                      strokeWidth={isSelected ? 2 : 1}
                      onMouseDown={onMouseDown}
                    />
                  )
                }
                if (shape.type === 'polygon' || shape.type === 'polyline') {
                  const points = shape.points.map(([x, y]) => `${x},${y}`).join(' ')
                  const Tag = shape.type === 'polygon' ? 'polygon' : 'polyline'
                  return (
                    <Tag
                      key={shape.id}
                      data-testid={`pdf-${shape.type}-${shape.id}`}
                      points={points}
                      fill={shape.type === 'polygon' ? color : 'none'}
                      fillOpacity={0.1}
                      stroke={color}
                      strokeWidth={isSelected ? 2 : 1}
                      onMouseDown={onMouseDown}
                    />
                  )
                }
                if (shape.type === 'span') {
                  const boxes = shape.boxes ?? []
                  return (
                    <g
                      key={shape.id}
                      data-testid={`pdf-span-${shape.id}`}
                      onMouseDown={onMouseDown}
                      style={{ cursor: 'pointer' }}
                    >
                      {boxes.map(([x1, y1, x2, y2], index) => (
                        <rect
                          key={index}
                          x={x1}
                          y={y1}
                          width={x2 - x1}
                          height={y2 - y1}
                          fill={color}
                          fillOpacity={isSelected ? 0.45 : 0.3}
                          stroke={isSelected ? color : 'none'}
                          strokeWidth={1}
                        />
                      ))}
                      {boxes[0] && (
                        <text
                          x={boxes[0][0]}
                          y={boxes[0][1] - 1}
                          fontSize={7}
                          fill={color}
                          style={{ pointerEvents: 'none' }}
                        >
                          {labelOf(shape.class)}
                        </text>
                      )}
                    </g>
                  )
                }
                if (shape.type === 'point') {
                  return (
                    <circle
                      key={shape.id}
                      data-testid={`pdf-point-${shape.id}`}
                      cx={shape.point[0]}
                      cy={shape.point[1]}
                      r={isSelected ? 4 : 3}
                      fill={color}
                      onMouseDown={onMouseDown}
                    />
                  )
                }
                return null
              })}
              {relations.map((relation) => {
                const from = shapeById.get(relation.from)
                const to = shapeById.get(relation.to)
                // Only arrows with both ends on this page are drawn; the rest are listed.
                if (!from || !to || (from.page ?? 1) !== pageNumber || (to.page ?? 1) !== pageNumber)
                  return null
                const a = shapeAnchor(from)
                const b = shapeAnchor(to)
                if (!a || !b) return null
                const color = colorFor(classes, relation.class)
                const width = relation.id === selectedId ? 2.5 : 1.5
                return (
                  <g
                    key={relation.id}
                    data-testid={`pdf-relation-${relation.id}`}
                    style={{ cursor: 'pointer' }}
                    onMouseDown={(event) => {
                      event.stopPropagation()
                      select(relation.id)
                    }}
                  >
                    <line
                      x1={a[0]}
                      y1={a[1]}
                      x2={b[0]}
                      y2={b[1]}
                      stroke={color}
                      strokeWidth={width}
                    />
                    <line
                      x1={a[0]}
                      y1={a[1]}
                      x2={b[0]}
                      y2={b[1]}
                      stroke="transparent"
                      strokeWidth={8}
                    />
                    <polygon points={arrowHead(a, b, 7)} fill={color} />
                  </g>
                )
              })}
              {spanSel && currentClass && (
                <g data-testid="pdf-span-preview" style={{ pointerEvents: 'none' }}>
                  {lineBoxes(wordRun(words, spanSel.start, spanSel.end)).map(
                    ([x1, y1, x2, y2], index) => (
                      <rect
                        key={index}
                        x={x1}
                        y={y1}
                        width={x2 - x1}
                        height={y2 - y1}
                        fill={colorFor(classes, currentClass)}
                        fillOpacity={0.3}
                        stroke={colorFor(classes, currentClass)}
                        strokeDasharray="3 2"
                      />
                    ),
                  )}
                </g>
              )}
              {!readOnly &&
                selected &&
                !isRelation(selected) &&
                (selected.page ?? 1) === pageNumber &&
                (() => {
                  const shape = edit?.original.id === selected.id ? edit.preview : selected
                  const ppp = pointsPerPixel()
                  const size = HANDLE_PX * ppp
                  const color = colorFor(classes, shape.class)
                  return shapeHandles(shape, 1 / ppp).map((handle) => (
                    <rect
                      key={handle.index}
                      data-testid={`pdf-handle-${handle.index}`}
                      x={handle.point[0] - size / 2}
                      y={handle.point[1] - size / 2}
                      width={size}
                      height={size}
                      fill={handle.kind === 'insert' ? 'white' : color}
                      fillOpacity={handle.kind === 'insert' ? 0.7 : 1}
                      stroke={color}
                      strokeWidth={ppp}
                      style={{ cursor: handle.kind === 'insert' ? 'copy' : 'move' }}
                      onMouseDown={(event) => startEdit(event, selected, handle.index)}
                    />
                  ))
                })()}
              {draft.length > 0 && (
                <polyline
                  data-testid="pdf-polygon-draft"
                  points={[...draft, ...(hover ? [hover] : [])]
                    .map(([x, y]) => `${x},${y}`)
                    .join(' ')}
                  fill="none"
                  stroke={currentClass ? colorFor(classes, currentClass) : '#94a3b8'}
                  strokeWidth={1.5}
                  strokeDasharray="4 3"
                />
              )}
              {drag && (
                <rect
                  data-testid="pdf-draw-preview"
                  x={Math.min(drag.start[0], drag.current[0])}
                  y={Math.min(drag.start[1], drag.current[1])}
                  width={Math.abs(drag.current[0] - drag.start[0])}
                  height={Math.abs(drag.current[1] - drag.start[1])}
                  fill="none"
                  strokeWidth={1.5}
                  stroke={currentClass ? colorFor(classes, currentClass) : '#94a3b8'}
                />
              )}
            </svg>
          </div>
        </div>
      ) : (
        <Spinner label={t('pdf.loadingPage')} />
      )}

      {selected && (
        <div
          className="w-full max-w-xl rounded border border-line p-2 text-sm text-ink"
          data-testid="pdf-selection"
        >
          <div className="flex flex-wrap items-center gap-2">
            {readOnly ? (
              <span className="font-medium">{selected.class}</span>
            ) : (
              <select
                aria-label={t('pdf.selectedClass')}
                className="rounded border border-line bg-surface px-1 py-0.5 text-ink"
                value={selected.class}
                onChange={(event) => reclassSelected(event.target.value)}
              >
                {classesFor(selected.type).map((c) => (
                  <option key={c.name} value={c.name}>
                    {c.display_name}
                  </option>
                ))}
              </select>
            )}
            {!readOnly && (
              <button
                type="button"
                className="rounded border border-line px-2 py-0.5"
                onClick={deleteSelected}
              >
                {t('pdf.delete')}
              </button>
            )}
          </div>
          <p className="mt-2 text-muted">
            {isRelation(selected) ? (
              <span data-testid="pdf-selection-relation">
                {describe(selected.from)} —{labelOf(selected.class)}→ {describe(selected.to)}
              </span>
            ) : (selected.type === 'bbox' || selected.type === 'span') && selected.text ? (
              <>
                <span className="text-xs uppercase">{t('pdf.text')}</span>{' '}
                <span className="text-ink" data-testid="pdf-selection-text">
                  {selected.text}
                </span>
              </>
            ) : (
              t('pdf.noText')
            )}
          </p>
        </div>
      )}
      {relations.length > 0 && (
        <RelationList
          relations={relations}
          selectedId={selectedId}
          readOnly={readOnly}
          labelOf={labelOf}
          colorOf={(name) => colorFor(classes, name)}
          describe={describe}
          onSelect={select}
          onDelete={(id) => {
            setShapes(removeShape(value.shapes, id))
            if (selectedId === id) select(null)
          }}
        />
      )}
      {!readOnly && <p className="text-xs text-muted">{t('pdf.hint')}</p>}
    </div>
  )
}
