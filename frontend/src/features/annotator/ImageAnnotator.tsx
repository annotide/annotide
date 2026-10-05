/**
 * Image annotation canvas (TOOL-1, IMG-3, IMG-4, UX-1, UX-2).
 *
 * Shapes in `value` and `onChange` are always in ORIGINAL IMAGE PIXELS, origin
 * top-left (DATA-8). The Konva layer is translated and scaled by the viewport,
 * so shape nodes are positioned in image coordinates directly and never need
 * per-shape conversion.
 */

import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import type Konva from 'konva'
import { Trans, useTranslation } from 'react-i18next'
import { Circle, Image as KonvaImage, Layer, Line, Rect, Stage, Text } from 'react-konva'

import { ImageAdjustFilter, ImageAdjustPanel, useImageAdjustStore } from './ImageAdjust'
import { isNeutral } from './adjustment'
import { formatLength, length, PIXELS } from './measure'
import type { PixelScale } from './measure'
import { RulerPanel } from './RulerPanel'
import { MAX_BRUSH_SIZE, MIN_BRUSH_SIZE, Toolbar } from './Toolbar'
import { dragHandle, removeVertex, translateShape } from './edit'
import { distance, isShapeVisible, rboxCorners, rboxFromThreePoints } from './geometry'
import { canBrush } from './mask'
import { EditHandles } from './shapes/EditHandles'
import { SuperpixelOverlay, useSuperpixels } from './SuperpixelLayer'
import { DEFAULT_SEGMENTS, fillSuperpixels, labelAt, labelsAlong } from './superpixels'
import { ShapeNode } from './shapes/ShapeNode'
import { TiledImage } from './TiledImage'
import type {
  AnnotationResult,
  BBox,
  ItemTiles,
  LabelClass,
  Point2D,
  Shape,
  Skeleton,
  SmartPrompt,
  TileKey,
  Tool,
} from './types'
import { useDrawing } from './useDrawing'
import { useViewport } from './useViewport'

const FALLBACK_COLOR = '#64748b'

/** Preview of the three-click rotated box: a line while the first side is
 * being placed, the full outline once the pointer sets the far side. */
function rboxDraftOutline(points: Point2D[], current: Point2D): Point2D[] {
  if (points.length < 2) return [points[0], current]
  const geometry = rboxFromThreePoints(points[0], points[1], current)
  if (!geometry || geometry.size[1] === 0) return [points[0], points[1]]
  return rboxCorners(geometry.center, geometry.size, geometry.angle)
}
const WHEEL_ZOOM_STEP = 1.1
/** Screen pixels the two clicks of a double-click may be apart. */
const DOUBLE_CLICK_TOLERANCE_PX = 6
/** Screen pixels a press must travel before it edits: a plain click on a
 * shape only selects it. */
const EDIT_DRAG_THRESHOLD_PX = 3
/** Arrow-key nudge of the selected shape, image pixels (Shift: the larger). */
const NUDGE_PX = 1
const NUDGE_SHIFT_PX = 10

/** Keyboard crosshair step (UX-7, WCAG 2.1.1), in screen pixels so it feels
 * the same at any zoom; Shift takes the larger. */
const KEY_CURSOR_STEP_PX = 10
const KEY_CURSOR_SHIFT_STEP_PX = 50

/** Tools a click drives, so the keyboard crosshair can too. Brush, eraser and
 * superpixel strokes follow the pointer's path (WCAG 2.1.1's exception);
 * smart polygon and the ruler stay pointer-only for now. */
const KEYBOARD_TOOLS: ReadonlySet<Tool> = new Set<Tool>([
  'bbox',
  'rbox',
  'polygon',
  'polyline',
  'point',
  'keypoints',
])

/** A select-tool edit in progress: the shape as it was when pressed, and the
 * handle being dragged (null: the whole shape is being moved). */
interface EditGesture {
  original: Shape
  handle: number | null
  start: Point2D
  moved: boolean
}

/** What the last press on the shapes layer hit, recorded by the shape or
 * handle and read by the stage handler the same event bubbles to next. */
type Pressed = { shapeId: string } | { handle: number; alt: boolean }

/** A one-off selection from outside the canvas; see `selectRequest`. */
export interface SelectRequest {
  id: string | null
  seq: number
}

export interface ImageAnnotatorProps {
  imageUrl: string
  imageWidth: number
  imageHeight: number
  classes: LabelClass[]
  value: AnnotationResult
  onChange: (next: AnnotationResult) => void
  readOnly?: boolean
  /** Fires with the selected shape id (or null) so the page can show an
   * attribute editor for it outside the canvas (TOOL-2). */
  onSelectionChange?: (id: string | null) => void
  /** Selects `id` (null: nothing) each time `seq` changes — how the page's
   * shape list makes shapes selectable without a pointer (UX-7). A request,
   * not a mirror of the selection, so the page's copy of the selection can
   * never be played back over a newer one. */
  selectRequest?: SelectRequest | null
  /** Smart polygon (ML-7): asked with the click or box the person made, it
   * resolves to the polygon a segment model proposed (`null` = nothing to
   * add). Absent when the organisation has no segment model — the tool is
   * then not offered at all. Failures are the caller's to report; the
   * promise rejecting only ends the "Segmenting…" state here. */
  onSmartPrompt?: (prompt: SmartPrompt) => Promise<Point2D[] | null>
  /** Size of a pixel for the ruler (TOOL-8); pixels when absent. */
  measureScale?: PixelScale
  /** Offered after a ruler measurement: the person states its true length,
   * and the caller receives units per pixel (isotropic) and the unit. */
  onCalibrate?: (unitsPerPixel: number, unit: string) => void
  /** A region task's region (IMG-6): the view opens on it and the rest of
   * the image is dimmed. Drawing outside it is not blocked here; the page
   * warns and the server refuses shapes anchored outside. */
  region?: BBox | null
  /** DZI tile pyramid (IMG-1): when set, the media layer shows tiles for the
   * zoom level the viewport needs instead of loading `imageUrl` at all. */
  tiles?: ItemTiles | null
  /** The item's id, used only to key the tile cache so switching items
   * resets it even when the caller does not remount the component. */
  itemId?: string
  /** Signs a batch (<=512) of `[level, col, row]` tiles and resolves to
   * their signed read URLs, in request order (IMG-1). Required when `tiles`
   * is set. */
  signTiles?: (tiles: TileKey[]) => Promise<string[]>
}

type MediaStatus = 'idle' | 'loading' | 'ready' | 'error'

/** Load the image element Konva draws, reporting how that went.

A failed load used to be indistinguishable from a slow one: both left the
canvas blank with the drawing tools live, so an annotator could draw boxes
onto nothing. The status drives a visible message instead. */
function useImageElement(url: string): [HTMLImageElement | null, MediaStatus] {
  const [element, setElement] = useState<HTMLImageElement | null>(null)
  const [status, setStatus] = useState<MediaStatus>(url ? 'loading' : 'idle')

  useEffect(() => {
    if (!url) {
      setElement(null)
      setStatus('idle')
      return
    }
    setElement(null)
    setStatus('loading')
    const image = new window.Image()
    // Media comes from the customer's storage on another origin via a signed
    // URL, so the request must not carry cookies.
    image.crossOrigin = 'anonymous'
    let cancelled = false
    image.onload = () => {
      if (cancelled) return
      setElement(image)
      setStatus('ready')
    }
    image.onerror = () => {
      if (cancelled) return
      setElement(null)
      setStatus('error')
    }
    image.src = url
    return () => {
      cancelled = true
    }
  }, [url])

  return [element, status]
}

/** Track the container's pixel size so the stage fills it. */
function useElementSize(): [React.RefObject<HTMLDivElement>, { width: number; height: number }] {
  const ref = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState({ width: 0, height: 0 })

  useEffect(() => {
    const node = ref.current
    if (!node) return
    const observer = new ResizeObserver(([entry]) => {
      const box = entry.contentRect
      setSize({ width: Math.floor(box.width), height: Math.floor(box.height) })
    })
    observer.observe(node)
    return () => observer.disconnect()
  }, [])

  return [ref, size]
}

/** The up-to-four rectangles `[x, y, w, h]` of the image outside `region`. */
export function regionShades(
  region: BBox,
  imageWidth: number,
  imageHeight: number,
): [number, number, number, number][] {
  const [xMin, yMin, xMax, yMax] = region
  const shades: [number, number, number, number][] = [
    [0, 0, imageWidth, yMin],
    [0, yMax, imageWidth, imageHeight - yMax],
    [0, yMin, xMin, yMax - yMin],
    [xMax, yMin, imageWidth - xMax, yMax - yMin],
  ]
  return shades.filter(([, , w, h]) => w > 0 && h > 0)
}

/** Which skeleton point the next click places, and how to skip or finish. */
function KeypointPrompt({
  className,
  names,
  placed,
}: {
  className: string
  names: string[]
  placed: number
}) {
  const { t } = useTranslation('annotator')
  return (
    <p
      role="status"
      className="pointer-events-none absolute left-2 top-2 z-10 rounded bg-surface/90 px-2 py-1
        text-xs text-ink shadow"
    >
      <Trans
        t={t}
        i18nKey="keypoints.prompt"
        values={{ className, name: names[placed], placed: placed + 1, total: names.length }}
        components={{ strong: <strong /> }}
      />
    </p>
  )
}

/** Brush diameter in screen pixels until the annotator changes it. */
const DEFAULT_BRUSH_SIZE = 24

export function ImageAnnotator({
  imageUrl,
  imageWidth,
  imageHeight,
  classes,
  value,
  onChange,
  readOnly = false,
  onSelectionChange,
  selectRequest = null,
  onSmartPrompt,
  measureScale,
  onCalibrate,
  region = null,
  tiles = null,
  itemId,
  signTiles,
}: ImageAnnotatorProps) {
  const { t } = useTranslation('annotator')
  const [containerRef, stageSize] = useElementSize()
  // A tiled item never loads the whole image (IMG-1); the media layer draws
  // `TiledImage` instead, so no full-image request is made at all.
  const [image, rawMediaStatus] = useImageElement(tiles ? '' : imageUrl)
  const mediaStatus: MediaStatus = tiles ? 'ready' : rawMediaStatus
  // One prompt in flight at a time: a second click while the model is still
  // answering would race two polygons onto the canvas in arrival order.
  const [segmenting, setSegmenting] = useState(false)
  const segmentingRef = useRef(false)

  const viewport = useViewport({
    imageWidth,
    imageHeight,
    stageWidth: stageSize.width,
    stageHeight: stageSize.height,
    focus: region,
  })

  const drawing = useDrawing({ value, onChange, classes, imageWidth, imageHeight, readOnly })

  // Post-draw editing (select tool): a press on a shape selects and moves it,
  // a press on one of the selected shape's handles drags that handle. The
  // preview replaces the shape on the canvas; release commits it once.
  const pressedRef = useRef<Pressed | null>(null)
  const editRef = useRef<EditGesture | null>(null)
  const [editPreview, setEditPreviewState] = useState<Shape | null>(null)
  const editPreviewRef = useRef<Shape | null>(null)
  const setEditPreview = useCallback((next: Shape | null) => {
    editPreviewRef.current = next
    setEditPreviewState(next)
  }, [])
  const canEdit = !readOnly && drawing.tool === 'select'
  const selectedShape = useMemo(
    () => value.shapes.find((s) => s.id === drawing.selectedId) ?? null,
    [drawing.selectedId, value.shapes],
  )
  // Stable across renders: every memoised ShapeNode receives it (IMG-4).
  const selectShape = drawing.select
  const handleShapeDown = useCallback(
    (id: string) => {
      pressedRef.current = { shapeId: id }
      selectShape(id)
    },
    [selectShape],
  )
  const handleHandleDown = useCallback((handle: number, alt: boolean) => {
    pressedRef.current = { handle, alt }
  }, [])
  const finishEdit = useCallback(() => {
    const gesture = editRef.current
    const preview = editPreviewRef.current
    editRef.current = null
    setEditPreview(null)
    if (gesture?.moved && preview) drawing.updateShape(preview)
  }, [drawing, setEditPreview])

  // The ruler (TOOL-8): one line, kept until the next measurement, Escape or
  // another tool. It is never part of the annotation.
  const [ruler, setRuler] = useState<{ from: Point2D; to: Point2D } | null>(null)

  // Keyboard crosshair (UX-7): arrows move it and Space places what a click
  // would, so every click-driven tool works without a pointer. Moving the
  // mouse over the stage hands control back to the pointer.
  const [keyCursor, setKeyCursorState] = useState<Point2D | null>(null)
  const keyCursorRef = useRef<Point2D | null>(null)
  const setKeyCursor = useCallback((next: Point2D | null) => {
    keyCursorRef.current = next
    setKeyCursorState(next)
  }, [])
  const rulerDraggingRef = useRef(false)
  const rulerScale = measureScale ?? PIXELS
  useEffect(() => {
    if (drawing.tool !== 'measure') setRuler(null)
  }, [drawing.tool])

  // Mask brush / eraser: the size is a screen diameter, so it feels the same
  // at every zoom; the stroke converts it to image pixels when it starts.
  // Tiled images are far too large to hold as a bitmap (IMG-1), and a schema
  // with no `mask` class has nothing the server would accept (QA-6).
  const brushEnabled =
    !readOnly &&
    !tiles &&
    canBrush(imageWidth, imageHeight) &&
    classes.some((cls) => cls.tools.includes('mask'))
  const [brushSize, setBrushSize] = useState(DEFAULT_BRUSH_SIZE)
  const [brushCursor, setBrushCursor] = useState<Point2D | null>(null)

  // Superpixels feed the same masks as the brush, so they share its
  // conditions (brushEnabled already rules out tiled images). SLIC runs on the
  // loaded image the first time the tool is picked. A gesture collects the
  // superpixels it crosses and paints them as one undo step on release.
  const superpixelsEnabled = brushEnabled
  const [segments, setSegments] = useState(DEFAULT_SEGMENTS)
  const superpixels = useSuperpixels(
    image,
    // The object, not its signature: a refetch re-signs the same media.
    imageUrl.split('?')[0],
    imageWidth,
    imageHeight,
    superpixelsEnabled && drawing.tool === 'superpixel',
    segments,
  )
  const segmentation = superpixels.segmentation
  const pickRef = useRef<{ erase: boolean; labels: Set<number>; last: Point2D } | null>(null)
  const [picked, setPicked] = useState<number[]>([])
  const [pickErase, setPickErase] = useState(false)
  const [hovered, setHovered] = useState(-1)
  const highlighted = useMemo(
    () => (hovered >= 0 && !picked.includes(hovered) ? [...picked, hovered] : picked),
    [hovered, picked],
  )

  const selectedId = drawing.selectedId
  const requestRef = useRef(selectRequest)
  requestRef.current = selectRequest
  useEffect(() => {
    if (requestRef.current) selectShape(requestRef.current.id)
  }, [selectRequest?.seq, selectShape])
  useEffect(() => {
    onSelectionChange?.(selectedId)
  }, [onSelectionChange, selectedId])

  const colorOf = useMemo(() => {
    const byName = new Map(classes.map((c) => [c.name, c.color]))
    return (name: string): string => byName.get(name) ?? FALLBACK_COLOR
  }, [classes])

  // Keypoints: offered when a class has a skeleton; bones come from it.
  const keypointsEnabled =
    !readOnly && classes.some((cls) => cls.skeleton && cls.tools.includes('keypoints'))
  const skeletonOf = useMemo(() => {
    const byName = new Map(classes.map((c) => [c.name, c.skeleton ?? null]))
    return (name: string): Skeleton | null => byName.get(name) ?? null
  }, [classes])

  // Virtualisation: only shapes intersecting the visible image rect are turned
  // into Konva nodes. With 10 000 shapes on a large image this is the
  // difference between a responsive canvas and a frozen one (IMG-4).
  const visibleShapes = useMemo(
    () => value.shapes.filter((shape) => isShapeVisible(shape, viewport.visibleRect, 8)),
    [value.shapes, viewport.visibleRect],
  )
  // Labels become unreadable clutter when zoomed far out, and drawing thousands
  // of them is the single most expensive thing on the layer.
  const showLabels = viewport.viewport.scale > 0.25 && visibleShapes.length <= 300

  const stagePoint = useCallback(
    (stage: { getPointerPosition: () => { x: number; y: number } | null }): Point2D | null => {
      const pointer = stage.getPointerPosition()
      return pointer ? [pointer.x, pointer.y] : null
    },
    [],
  )

  const handleWheel = useCallback(
    (event: { evt: WheelEvent; target: { getStage: () => unknown } }) => {
      event.evt.preventDefault()
      const stage = event.target.getStage() as {
        getPointerPosition: () => { x: number; y: number } | null
      } | null
      const pointer = stage?.getPointerPosition()
      if (!pointer) return
      const factor = event.evt.deltaY < 0 ? WHEEL_ZOOM_STEP : 1 / WHEEL_ZOOM_STEP
      viewport.zoomAt([pointer.x, pointer.y], factor)
    },
    [viewport],
  )

  const handleMouseDown = useCallback(
    (event: { evt: MouseEvent; target: { getStage: () => unknown } }) => {
      const stage = event.target.getStage() as {
        getPointerPosition: () => { x: number; y: number } | null
      } | null
      if (!stage) return
      const point = stagePoint(stage)
      if (!point) return
      // Recorded by a shape or handle this same press hit first (bubbling);
      // a press on the bare stage cannot have hit one (a stale tap could).
      const pressed = (event.target as unknown) === stage ? null : pressedRef.current
      pressedRef.current = null

      // Space-drag and middle mouse pan instead of drawing.
      if (viewport.isPanning || event.evt.button === 1) return
      if (drawing.tool === 'measure') {
        const start = viewport.toImage(point)
        rulerDraggingRef.current = true
        setRuler({ from: start, to: start })
        return
      }
      if (readOnly) return

      const imagePoint = viewport.toImage(point)
      switch (drawing.tool) {
        case 'bbox':
          drawing.beginBBoxDrag(imagePoint)
          break
        case 'rbox':
          drawing.addRBoxPoint(imagePoint)
          break
        case 'polygon':
          drawing.addPolygonPoint(imagePoint)
          break
        case 'polyline':
          drawing.addPolylinePoint(imagePoint)
          break
        case 'point':
          drawing.addPointShape(imagePoint)
          break
        case 'smart':
          if (!segmentingRef.current) drawing.beginSmartDrag(imagePoint)
          break
        case 'keypoints':
          if (event.evt.button === 0) drawing.addKeypoint(imagePoint, event.evt.altKey)
          break
        case 'brush':
        case 'eraser':
          if (event.evt.button !== 0) break
          drawing.beginStroke(
            imagePoint,
            brushSize / 2 / viewport.viewport.scale,
            drawing.tool === 'eraser' || event.evt.altKey,
          )
          break
        case 'superpixel': {
          if (event.evt.button !== 0 || !segmentation) break
          const label = labelAt(segmentation, imagePoint)
          const labels = new Set(label >= 0 ? [label] : [])
          pickRef.current = { erase: event.evt.altKey, labels, last: imagePoint }
          setPickErase(event.evt.altKey)
          setPicked([...labels])
          break
        }
        case 'select': {
          if (event.evt.button !== 0) break
          if (!pressed) {
            // Nothing recorded the press: it missed every shape.
            drawing.select(null)
            break
          }
          const original =
            'shapeId' in pressed
              ? value.shapes.find((s) => s.id === pressed.shapeId)
              : selectedShape
          if (!original) break
          if ('handle' in pressed && pressed.alt) {
            const next = removeVertex(original, pressed.handle)
            if (next) drawing.updateShape(next)
            break
          }
          editRef.current = {
            original,
            handle: 'handle' in pressed ? pressed.handle : null,
            start: imagePoint,
            moved: false,
          }
          break
        }
      }
    },
    [brushSize, drawing, readOnly, segmentation, selectedShape, stagePoint, value.shapes, viewport],
  )

  const handleMouseMove = useCallback(
    (event: { evt: MouseEvent; target: { getStage: () => unknown } }) => {
      const stage = event.target.getStage() as {
        getPointerPosition: () => { x: number; y: number } | null
      } | null
      if (!stage) return
      const point = stagePoint(stage)
      if (!point) return

      if (keyCursorRef.current) setKeyCursor(null)
      if (viewport.isPanning && event.evt.buttons > 0) {
        viewport.panBy(event.evt.movementX, event.evt.movementY)
        return
      }
      if (rulerDraggingRef.current) {
        const end = viewport.toImage(point)
        setRuler((current) => current && { ...current, to: end })
        return
      }
      const gesture = editRef.current
      if (gesture) {
        // Released outside the stage: the mouseup never reached us.
        if (event.evt.buttons === 0) {
          finishEdit()
          return
        }
        const at = viewport.toImage(point)
        const dx = at[0] - gesture.start[0]
        const dy = at[1] - gesture.start[1]
        if (!gesture.moved) {
          const travelled = Math.hypot(dx, dy) * viewport.viewport.scale
          if (travelled < EDIT_DRAG_THRESHOLD_PX) return
          gesture.moved = true
        }
        const next =
          gesture.handle === null
            ? translateShape(gesture.original, dx, dy, imageWidth, imageHeight)
            : dragHandle(gesture.original, gesture.handle, at, imageWidth, imageHeight)
        if (next) setEditPreview(next)
        return
      }
      if (drawing.tool === 'brush' || drawing.tool === 'eraser') {
        setBrushCursor(viewport.toImage(point))
      }
      if (drawing.tool === 'superpixel' && segmentation) {
        const at = viewport.toImage(point)
        setHovered(labelAt(segmentation, at))
        const pick = pickRef.current
        if (pick) {
          const size = pick.labels.size
          for (const label of labelsAlong(segmentation, pick.last, at)) pick.labels.add(label)
          pick.last = at
          if (pick.labels.size !== size) setPicked([...pick.labels])
        }
      }
      if (drawing.draft?.tool === 'brush') {
        drawing.extendStroke(viewport.toImage(point))
      } else if (drawing.draft?.tool === 'bbox') {
        drawing.updateBBoxDrag(viewport.toImage(point))
      } else if (drawing.draft?.tool === 'rbox') {
        drawing.updateRBoxPreview(viewport.toImage(point))
      } else if (drawing.draft?.tool === 'smart') {
        drawing.updateSmartDrag(viewport.toImage(point))
      }
    },
    [
      drawing,
      finishEdit,
      imageHeight,
      imageWidth,
      segmentation,
      setEditPreview,
      setKeyCursor,
      stagePoint,
      viewport,
    ],
  )

  const runSmartPrompt = useCallback(
    (prompt: SmartPrompt) => {
      if (!onSmartPrompt || segmentingRef.current) return
      segmentingRef.current = true
      setSegmenting(true)
      onSmartPrompt(prompt)
        .then((points) => {
          if (points && points.length > 0) drawing.addPolygonShape(points)
        })
        .catch(() => undefined)
        .finally(() => {
          segmentingRef.current = false
          setSegmenting(false)
        })
    },
    [drawing, onSmartPrompt],
  )

  const handleMouseUp = useCallback(() => {
    rulerDraggingRef.current = false
    if (editRef.current) {
      finishEdit()
      return
    }
    const pick = pickRef.current
    if (pick) {
      pickRef.current = null
      setPicked([])
      setPickErase(false)
      if (segmentation && pick.labels.size > 0) {
        drawing.paintMask(pick.erase, (bitmap, value) =>
          fillSuperpixels(bitmap, segmentation, pick.labels, value),
        )
      }
      return
    }
    if (drawing.draft?.tool === 'bbox') drawing.commitBBoxDrag()
    else if (drawing.draft?.tool === 'brush') drawing.commitStroke()
    else if (drawing.draft?.tool === 'smart') {
      const prompt = drawing.finishSmartDrag()
      if (prompt) runSmartPrompt(prompt)
    }
  }, [drawing, finishEdit, runSmartPrompt, segmentation])

  const handleDoubleClick = useCallback(() => {
    const draft = drawing.draft
    if (!draft || (draft.tool !== 'polygon' && draft.tool !== 'polyline')) return
    // Konva raises dblclick for any two clicks inside its time window, wherever
    // they land, so quickly placed vertices would otherwise finish the shape.
    // Both mousedowns already added a vertex; only when they sit on the same
    // spot is this a real double-click.
    const points = draft.points
    if (points.length >= 2) {
      const gap = distance(points[points.length - 1], points[points.length - 2])
      if (gap * viewport.viewport.scale > DOUBLE_CLICK_TOLERANCE_PX) return
    }
    if (draft.tool === 'polygon') drawing.closePolygon()
    else drawing.finishPolyline()
  }, [drawing, viewport.viewport.scale])

  const moveKeyCursor = useCallback(
    (key: string, large: boolean) => {
      const rect = viewport.visibleRect
      const from = keyCursorRef.current ?? [
        Math.min(imageWidth, Math.max(0, rect.x + rect.width / 2)),
        Math.min(imageHeight, Math.max(0, rect.y + rect.height / 2)),
      ]
      // The first press only shows the crosshair where it starts.
      let next: Point2D = from
      if (keyCursorRef.current) {
        const step =
          (large ? KEY_CURSOR_SHIFT_STEP_PX : KEY_CURSOR_STEP_PX) / viewport.viewport.scale
        const dx = key === 'ArrowLeft' ? -step : key === 'ArrowRight' ? step : 0
        const dy = key === 'ArrowUp' ? -step : key === 'ArrowDown' ? step : 0
        next = [
          Math.min(imageWidth, Math.max(0, from[0] + dx)),
          Math.min(imageHeight, Math.max(0, from[1] + dy)),
        ]
      }
      setKeyCursor(next)
      if (drawing.draft?.tool === 'bbox') drawing.updateBBoxDrag(next)
      else if (drawing.draft?.tool === 'rbox') drawing.updateRBoxPreview(next)
    },
    [drawing, imageHeight, imageWidth, setKeyCursor, viewport],
  )

  const placeAtKeyCursor = useCallback(
    (point: Point2D, alternate: boolean) => {
      switch (drawing.tool) {
        case 'bbox':
          // Two presses: the first corner, then the opposite one.
          if (drawing.draft?.tool === 'bbox') drawing.commitBBoxDrag()
          else drawing.beginBBoxDrag(point)
          break
        case 'rbox':
          drawing.addRBoxPoint(point)
          break
        case 'polygon':
          drawing.addPolygonPoint(point)
          break
        case 'polyline':
          drawing.addPolylinePoint(point)
          break
        case 'point':
          drawing.addPointShape(point)
          break
        case 'keypoints':
          drawing.addKeypoint(point, alternate)
          break
      }
    },
    [drawing],
  )

  // Keyboard: tools, zoom, undo/redo, delete, class hotkeys (UX-1, UX-2).
  //
  // The window listener is registered once and calls the latest handler
  // through a ref. Re-registering it per render loses keys: the viewport's
  // own Space listener runs first, React flushes its state update between
  // the two listeners, and a listener removed mid-dispatch never sees the
  // event — so Space never reached the keyboard crosshair (UX-7).
  const keyHandlerRef = useRef<(event: KeyboardEvent) => void>(() => undefined)
  useEffect(() => {
    const listener = (event: KeyboardEvent) => keyHandlerRef.current(event)
    window.addEventListener('keydown', listener)
    return () => window.removeEventListener('keydown', listener)
  }, [])
  useEffect(() => {
    keyHandlerRef.current = (event: KeyboardEvent) => {
      const target = event.target
      if (
        target instanceof HTMLElement &&
        (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))
      ) {
        return
      }

      const mod = event.metaKey || event.ctrlKey
      if (mod && event.key.toLowerCase() === 'z') {
        event.preventDefault()
        if (event.shiftKey) drawing.redo()
        else drawing.undo()
        return
      }
      if (mod) return

      const keyTool = !readOnly && KEYBOARD_TOOLS.has(drawing.tool)
      if (event.code === 'Space' && keyTool && keyCursorRef.current) {
        // A tap places; holding Space to pan only matters with a mouse.
        event.preventDefault()
        if (!event.repeat) placeAtKeyCursor(keyCursorRef.current, event.altKey || event.shiftKey)
        return
      }

      switch (event.key) {
        case 'Escape':
          drawing.cancelDraft()
          setRuler(null)
          setKeyCursor(null)
          return
        case 'Enter':
          if (drawing.draft?.tool === 'polygon') {
            event.preventDefault()
            drawing.closePolygon()
          } else if (drawing.draft?.tool === 'polyline') {
            event.preventDefault()
            drawing.finishPolyline()
          } else if (drawing.draft?.tool === 'keypoints') {
            event.preventDefault()
            drawing.finishKeypoints()
          }
          return
        case '[':
        case ']':
          if (drawing.tool === 'brush' || drawing.tool === 'eraser') {
            const factor = event.key === ']' ? 1.25 : 0.8
            setBrushSize((size) =>
              Math.min(MAX_BRUSH_SIZE, Math.max(MIN_BRUSH_SIZE, Math.round(size * factor))),
            )
          }
          return
        case 'Backspace':
          if (
            drawing.draft &&
            drawing.draft.tool !== 'bbox' &&
            drawing.draft.tool !== 'smart' &&
            drawing.draft.tool !== 'brush'
          ) {
            event.preventDefault()
            drawing.removeLastPolygonPoint()
          }
          return
        case 'Delete':
          drawing.deleteSelected()
          return
        case 'ArrowLeft':
        case 'ArrowRight':
        case 'ArrowUp':
        case 'ArrowDown': {
          if (keyTool && !editRef.current) {
            event.preventDefault()
            moveKeyCursor(event.key, event.shiftKey)
            return
          }
          if (!canEdit || !selectedShape || editRef.current) return
          event.preventDefault()
          const step = event.shiftKey ? NUDGE_SHIFT_PX : NUDGE_PX
          const dx = event.key === 'ArrowLeft' ? -step : event.key === 'ArrowRight' ? step : 0
          const dy = event.key === 'ArrowUp' ? -step : event.key === 'ArrowDown' ? step : 0
          const next = translateShape(selectedShape, dx, dy, imageWidth, imageHeight)
          if (next) drawing.updateShape(next)
          return
        }
        case '+':
        case '=':
          viewport.zoomIn()
          return
        case '-':
          viewport.zoomOut()
          return
        case '0':
          viewport.fitToStage()
          return
      }

      // Letter keys this canvas acts on are marked consumed (preventDefault)
      // so the page's fallback shortcuts — N next, P previous — stay out.
      // Tools take no Shift: Shift+N / Shift+P always reach the page.
      const key = event.key.toLowerCase()
      if (key === 'n' && !event.shiftKey && drawing.tool === 'keypoints') {
        event.preventDefault()
        drawing.skipKeypoint()
        return
      }
      const toolKeys: Record<string, Tool> = {
        v: 'select',
        b: 'bbox',
        r: 'rbox',
        g: 'polygon',
        l: 'polyline',
        p: 'point',
        m: 'measure',
        ...(onSmartPrompt ? { s: 'smart' as const } : {}),
        ...(brushEnabled ? { k: 'brush' as const, e: 'eraser' as const } : {}),
        ...(superpixelsEnabled ? { x: 'superpixel' as const } : {}),
        ...(keypointsEnabled ? { j: 'keypoints' as const } : {}),
      }
      if (key in toolKeys && !event.shiftKey) {
        // Read-only (review) leaves the key to the page: R there is "comment".
        if (!readOnly) event.preventDefault()
        drawing.setTool(toolKeys[key])
        return
      }
      if (key === 'i' && !classes.some((cls) => cls.hotkey === event.key)) {
        event.preventDefault()
        setAdjustOpen((open) => !open)
        return
      }
      // Anything left may be a class hotkey from the label schema.
      if (!readOnly && classes.some((cls) => cls.hotkey === event.key)) event.preventDefault()
      drawing.applyHotkeyClass(event.key)
    }

  }, [
    brushEnabled,
    canEdit,
    classes,
    drawing,
    imageHeight,
    imageWidth,
    keypointsEnabled,
    moveKeyCursor,
    onSmartPrompt,
    placeAtKeyCursor,
    readOnly,
    selectedShape,
    setKeyCursor,
    superpixelsEnabled,
    viewport,
  ])

  // Display adjustments (IMG-5): a CSS filter on the media layer's canvas only,
  // so the shapes above keep their true colours.
  const adjustment = useImageAdjustStore((state) => state.adjustment)
  const setAdjustment = useImageAdjustStore((state) => state.setAdjustment)
  const [adjustOpen, setAdjustOpen] = useState(false)
  const filterId = `image-adjust-${useId().replace(/:/g, '')}`
  const mediaLayerRef = useRef<Konva.Layer>(null)
  useEffect(() => {
    const canvas = mediaLayerRef.current?.getNativeCanvasElement()
    if (canvas) canvas.style.filter = isNeutral(adjustment) ? '' : `url(#${filterId})`
  })

  const { scale, offsetX, offsetY } = viewport.viewport
  const draft = drawing.draft
  const activeColor = colorOf(drawing.activeClassName ?? '')

  return (
    <div className="flex h-full min-h-0 flex-col">
      <Toolbar
        tool={drawing.tool}
        onToolChange={drawing.setTool}
        onZoomIn={viewport.zoomIn}
        onZoomOut={viewport.zoomOut}
        onFit={viewport.fitToStage}
        onUndo={drawing.undo}
        onRedo={drawing.redo}
        canUndo={drawing.canUndo}
        canRedo={drawing.canRedo}
        scale={scale}
        readOnly={readOnly}
        smartEnabled={Boolean(onSmartPrompt)}
        adjustOpen={adjustOpen}
        onToggleAdjust={() => setAdjustOpen((open) => !open)}
        adjusted={!isNeutral(adjustment)}
        keypointsEnabled={keypointsEnabled}
        brushEnabled={brushEnabled}
        brushSize={brushSize}
        onBrushSizeChange={setBrushSize}
        superpixelsEnabled={superpixelsEnabled}
        superpixelSegments={segments}
        onSuperpixelSegmentsChange={setSegments}
      />
      {adjustOpen && <ImageAdjustPanel value={adjustment} onChange={setAdjustment} />}
      <ImageAdjustFilter id={filterId} adjustment={adjustment} />

      <div
        ref={containerRef}
        className="relative min-h-0 flex-1 overflow-hidden bg-[#101418]"
        data-superpixels={drawing.tool === 'superpixel' ? superpixels.status : undefined}
        style={{
          cursor: viewport.isPanning
            ? 'grab'
            : editPreview
              ? 'move'
              : drawing.tool === 'select'
                ? 'default'
                : 'crosshair',
        }}
      >
        {ruler && drawing.tool === 'measure' && (
          <RulerPanel
            pixels={length(ruler.from, ruler.to, PIXELS)}
            measured={length(ruler.from, ruler.to, rulerScale)}
            scale={rulerScale}
            onCalibrate={onCalibrate}
          />
        )}

        {mediaStatus === 'loading' && (
          <p
            role="status"
            className="pointer-events-none absolute inset-0 z-10 flex items-center
              justify-center text-sm text-muted"
          >
            {t('canvas.loadingImage')}
          </p>
        )}

        {drawing.tool === 'keypoints' && drawing.keypointsClass?.skeleton && (
          <KeypointPrompt
            className={drawing.keypointsClass.display_name}
            names={drawing.keypointsClass.skeleton.points}
            placed={draft?.tool === 'keypoints' ? draft.points.length : 0}
          />
        )}
        {superpixels.status === 'computing' && (
          <p
            role="status"
            className="pointer-events-none absolute left-2 top-2 z-10 rounded bg-surface/90
              px-2 py-1 text-xs text-ink shadow"
          >
            {t('canvas.computingSuperpixels')}
          </p>
        )}
        {superpixels.status === 'error' && (
          <p
            role="alert"
            className="pointer-events-none absolute left-2 top-2 z-10 max-w-sm rounded
              bg-surface/90 px-2 py-1 text-xs text-ink shadow"
          >
            {t('canvas.superpixelsError')}
          </p>
        )}
        {segmenting && (
          <p
            role="status"
            className="pointer-events-none absolute left-2 top-2 z-10 rounded bg-surface/90
              px-2 py-1 text-xs text-ink shadow"
          >
            {t('canvas.segmenting')}
          </p>
        )}

        {keyCursor && !readOnly && KEYBOARD_TOOLS.has(drawing.tool) && (
          <p
            role="status"
            className="pointer-events-none absolute bottom-2 left-2 z-10 rounded bg-surface/90
              px-2 py-1 text-xs text-ink shadow"
          >
            {t('canvas.keyCursor', {
              x: Math.round(keyCursor[0]),
              y: Math.round(keyCursor[1]),
            })}
          </p>
        )}

        {mediaStatus === 'error' && (
          <div
            role="alert"
            className="pointer-events-none absolute inset-0 z-10 flex flex-col items-center
              justify-center gap-1 p-4 text-center"
          >
            <p className="text-sm text-ink">{t('canvas.imageLoadError')}</p>
            <p className="text-xs text-muted">{t('canvas.imageLoadErrorHint')}</p>
          </div>
        )}

        {stageSize.width > 0 && stageSize.height > 0 && (
          <Stage
            width={stageSize.width}
            height={stageSize.height}
            onWheel={handleWheel}
            onMouseDown={handleMouseDown}
            onMouseMove={handleMouseMove}
            onMouseUp={handleMouseUp}
            onMouseLeave={() => {
              setBrushCursor(null)
              setHovered(-1)
            }}
            onDblClick={handleDoubleClick}
          >
            {/* Media layer: never interactive, so hit detection skips it. */}
            <Layer
              ref={mediaLayerRef}
              listening={false}
              x={offsetX}
              y={offsetY}
              scaleX={scale}
              scaleY={scale}
            >
              {tiles && signTiles
                ? // Only once fitted: the placeholder viewport (scale 1) would
                  // sign full-resolution tiles nobody is going to see (IMG-1).
                  viewport.fitted && (
                    <TiledImage
                      key={itemId ?? ''}
                      meta={tiles}
                      scale={scale}
                      visibleRect={viewport.visibleRect}
                      signTiles={signTiles}
                    />
                  )
                : !tiles &&
                  image && <KonvaImage image={image} width={imageWidth} height={imageHeight} />}
            </Layer>

            {segmentation && drawing.tool === 'superpixel' && (
              <Layer
                listening={false}
                imageSmoothingEnabled={false}
                x={offsetX}
                y={offsetY}
                scaleX={scale}
                scaleY={scale}
              >
                <SuperpixelOverlay
                  segmentation={segmentation}
                  imageWidth={imageWidth}
                  imageHeight={imageHeight}
                  picked={highlighted}
                  color={
                    pickErase
                      ? '#ffffff'
                      : selectedShape?.type === 'mask'
                        ? colorOf(selectedShape.class)
                        : activeColor
                  }
                />
              </Layer>
            )}

            {region && (
              <Layer listening={false} x={offsetX} y={offsetY} scaleX={scale} scaleY={scale}>
                {regionShades(region, imageWidth, imageHeight).map(([x, y, w, h]) => (
                  <Rect
                    key={`${x},${y}`}
                    x={x}
                    y={y}
                    width={w}
                    height={h}
                    fill="black"
                    opacity={0.45}
                    perfectDrawEnabled={false}
                  />
                ))}
                <Rect
                  x={region[0]}
                  y={region[1]}
                  width={region[2] - region[0]}
                  height={region[3] - region[1]}
                  stroke="#facc15"
                  strokeWidth={2 / scale}
                  dash={[6 / scale, 4 / scale]}
                  perfectDrawEnabled={false}
                />
              </Layer>
            )}

            <Layer x={offsetX} y={offsetY} scaleX={scale} scaleY={scale}>
              {visibleShapes.map((committed) => {
                const shape = editPreview?.id === committed.id ? editPreview : committed
                return (
                  <ShapeNode
                    key={shape.id}
                    shape={shape}
                    color={colorOf(shape.class)}
                    scale={scale}
                    selected={shape.id === drawing.selectedId}
                    showLabel={showLabels}
                    listening={!readOnly || drawing.tool === 'select'}
                    onSelect={handleShapeDown}
                    edges={shape.type === 'keypoints' ? skeletonOf(shape.class)?.edges : undefined}
                  />
                )
              })}
              {canEdit && selectedShape && (
                <EditHandles
                  shape={editPreview?.id === selectedShape.id ? editPreview : selectedShape}
                  color={colorOf(selectedShape.class)}
                  scale={scale}
                  onHandleDown={handleHandleDown}
                />
              )}

              {/* In-progress drawing, drawn above the committed shapes. */}
              {(draft?.tool === 'bbox' || draft?.tool === 'smart') && (
                <Rect
                  x={Math.min(draft.start[0], draft.current[0])}
                  y={Math.min(draft.start[1], draft.current[1])}
                  width={Math.abs(draft.current[0] - draft.start[0])}
                  height={Math.abs(draft.current[1] - draft.start[1])}
                  stroke={activeColor}
                  strokeWidth={1.5 / scale}
                  dash={[4 / scale, 3 / scale]}
                  listening={false}
                  perfectDrawEnabled={false}
                />
              )}
              {(draft?.tool === 'polygon' || draft?.tool === 'polyline') &&
                draft.points.length > 0 && (
                  <Line
                    points={draft.points.flat()}
                    stroke={activeColor}
                    strokeWidth={1.5 / scale}
                    dash={[4 / scale, 3 / scale]}
                    listening={false}
                    perfectDrawEnabled={false}
                  />
                )}
              {ruler && (
                <>
                  <Line
                    points={[...ruler.from, ...ruler.to]}
                    stroke="#facc15"
                    strokeWidth={2 / scale}
                    dash={[6 / scale, 4 / scale]}
                    listening={false}
                    perfectDrawEnabled={false}
                  />
                  <Text
                    x={ruler.to[0] + 8 / scale}
                    y={ruler.to[1] + 8 / scale}
                    text={formatLength(length(ruler.from, ruler.to, rulerScale), rulerScale)}
                    fontSize={14 / scale}
                    fill="#facc15"
                    listening={false}
                  />
                </>
              )}
              {draft?.tool === 'keypoints' && (
                <ShapeNode
                  shape={{
                    id: 'keypoints-draft',
                    type: 'keypoints',
                    class: draft.className,
                    attributes: {},
                    confidence: null,
                    points: draft.points,
                  }}
                  color={colorOf(draft.className)}
                  scale={scale}
                  selected
                  showLabel={false}
                  listening={false}
                  edges={skeletonOf(draft.className)?.edges}
                />
              )}
              {draft?.tool === 'brush' && (
                <Line
                  points={
                    draft.points.length === 1 ? [...draft.points[0], ...draft.points[0]] : draft.points.flat()
                  }
                  stroke={draft.erase ? '#ffffff' : activeColor}
                  strokeWidth={draft.radius * 2}
                  lineCap="round"
                  lineJoin="round"
                  opacity={0.5}
                  listening={false}
                  perfectDrawEnabled={false}
                />
              )}
              {brushCursor && (drawing.tool === 'brush' || drawing.tool === 'eraser') && (
                <Circle
                  x={brushCursor[0]}
                  y={brushCursor[1]}
                  radius={brushSize / 2 / scale}
                  stroke={drawing.tool === 'eraser' ? '#ffffff' : activeColor}
                  strokeWidth={1 / scale}
                  listening={false}
                  perfectDrawEnabled={false}
                />
              )}
              {draft?.tool === 'rbox' && (
                <Line
                  points={rboxDraftOutline(draft.points, draft.current).flat()}
                  closed={draft.points.length === 2}
                  stroke={activeColor}
                  strokeWidth={1.5 / scale}
                  dash={[4 / scale, 3 / scale]}
                  listening={false}
                  perfectDrawEnabled={false}
                />
              )}
              {keyCursor && !readOnly && KEYBOARD_TOOLS.has(drawing.tool) && (
                <Line
                  points={[
                    keyCursor[0] - 12 / scale,
                    keyCursor[1],
                    keyCursor[0] + 12 / scale,
                    keyCursor[1],
                  ]}
                  stroke={activeColor}
                  strokeWidth={2 / scale}
                  listening={false}
                />
              )}
              {keyCursor && !readOnly && KEYBOARD_TOOLS.has(drawing.tool) && (
                <Line
                  points={[
                    keyCursor[0],
                    keyCursor[1] - 12 / scale,
                    keyCursor[0],
                    keyCursor[1] + 12 / scale,
                  ]}
                  stroke={activeColor}
                  strokeWidth={2 / scale}
                  listening={false}
                />
              )}
            </Layer>
          </Stage>
        )}
      </div>
    </div>
  )
}
