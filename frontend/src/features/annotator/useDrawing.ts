/**
 * Drawing, selection and undo/redo state for the annotator.
 *
 * This hook owns everything that mutates `AnnotationResult` (bbox/rbox/
 * polygon/polyline/point creation, moving, resizing, deleting, class
 * assignment) plus the transient "in progress" draft for the drag and
 * click-sequence tools. It is deliberately
 * framework-light: all the tricky logic (bbox normalisation, degenerate
 * rejection, polygon closing rules, history stack semantics) is exercised
 * directly in useDrawing.test.ts via renderHook, with no Konva/canvas involved.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  clampBBoxToImage,
  clampPointToImage,
  isDegenerateBBox,
  normalizeBBox,
  rboxFromThreePoints,
} from './geometry'
import { canBrush, decodeRle, emptyBitmap, encodeRle, isEmptyRle, paintStroke } from './mask'
import type { Bitmap } from './mask'
import { newShapeId } from '@/lib/ids'
import type {
  AnnotationResult,
  Keypoint,
  LabelAttribute,
  LabelClass,
  Point2D,
  Shape,
  ShapeAttributes,
  SmartPrompt,
  Tool,
} from './types'

// ---------------------------------------------------------------------------
// Attribute defaults (TOOL-2). A new shape starts with every attribute that
// declares a `default`, so a required boolean/select is valid on submit
// without the annotator opening the editor for each box.
// ---------------------------------------------------------------------------

/** Attributes a freshly created shape of `cls` starts with. */
export function defaultAttributes(cls: LabelClass | undefined): ShapeAttributes {
  const out: ShapeAttributes = {}
  for (const attr of cls?.attributes ?? []) {
    if (attr.default !== undefined && attr.default !== null) out[attr.name] = attr.default
  }
  return out
}

/** Attributes for a shape re-classified to `cls`: keep the values whose
 * definition exists on the new class, fill the rest from defaults. */
export function reconcileAttributes(
  attributes: ShapeAttributes,
  cls: LabelClass | undefined,
): ShapeAttributes {
  const defs = new Map<string, LabelAttribute>((cls?.attributes ?? []).map((a) => [a.name, a]))
  const out = defaultAttributes(cls)
  for (const [name, value] of Object.entries(attributes)) {
    if (defs.has(name)) out[name] = value
  }
  return out
}

// ---------------------------------------------------------------------------
// Generic, pure undo/redo history stack (UX-2). Kept free of React so it is
// trivial to reason about and test: a commit pushes one entry, never per
// mouse-move, and a fresh commit after an undo drops the redo branch.
// ---------------------------------------------------------------------------

export interface HistoryState<T> {
  past: T[]
  present: T
  future: T[]
}

export function createHistory<T>(present: T): HistoryState<T> {
  return { past: [], present, future: [] }
}

export function pushHistory<T>(history: HistoryState<T>, next: T): HistoryState<T> {
  if (next === history.present) return history
  return { past: [...history.past, history.present], present: next, future: [] }
}

export function undoHistory<T>(history: HistoryState<T>): HistoryState<T> {
  if (history.past.length === 0) return history
  const previous = history.past[history.past.length - 1]
  return {
    past: history.past.slice(0, -1),
    present: previous,
    future: [history.present, ...history.future],
  }
}

export function redoHistory<T>(history: HistoryState<T>): HistoryState<T> {
  if (history.future.length === 0) return history
  const [next, ...rest] = history.future
  return { past: [...history.past, history.present], present: next, future: rest }
}

// ---------------------------------------------------------------------------
// In-progress draft for the bbox / rbox / polygon / polyline tools
// ---------------------------------------------------------------------------

export type Draft =
  | { tool: 'bbox'; start: Point2D; current: Point2D }
  /** Three-click rotated box: `points` holds the 1-2 clicked ends of the
   * first side, `current` follows the pointer for the live preview. */
  | { tool: 'rbox'; points: Point2D[]; current: Point2D }
  | { tool: 'polygon'; points: Point2D[] }
  | { tool: 'polyline'; points: Point2D[] }
  /** Smart-polygon prompt in progress: a click that may grow into a box. */
  | { tool: 'smart'; start: Point2D; current: Point2D }
  /** Brush / eraser stroke: the pointer path and the radius, image pixels. */
  | { tool: 'brush'; erase: boolean; points: Point2D[]; radius: number }
  /** Skeleton in progress: the points placed (or skipped) so far, in the
   * order of `className`'s skeleton. */
  | { tool: 'keypoints'; className: string; points: Keypoint[] }
  | null

const MIN_BBOX_SIZE = 3
const MIN_POLYGON_POINTS = 3
const MIN_POLYLINE_POINTS = 2
/** Image pixels within which two consecutive vertices count as the same one. */
const DUPLICATE_VERTEX_PX = 1

/** Drops trailing vertices that repeat the one before them: a double-click
 * that finishes a path fires mousedown twice on the same spot. */
function trimTrailingDuplicates(points: Point2D[]): Point2D[] {
  let end = points.length
  while (end >= 2) {
    const [ax, ay] = points[end - 1]
    const [bx, by] = points[end - 2]
    if (Math.abs(ax - bx) > DUPLICATE_VERTEX_PX || Math.abs(ay - by) > DUPLICATE_VERTEX_PX) break
    end -= 1
  }
  return points.slice(0, end)
}


export interface UseDrawingOptions {
  value: AnnotationResult
  onChange: (next: AnnotationResult) => void
  classes: LabelClass[]
  imageWidth: number
  imageHeight: number
  readOnly?: boolean
}

export interface UseDrawingResult {
  tool: Tool
  setTool: (tool: Tool) => void
  draft: Draft
  selectedId: string | null
  select: (id: string | null) => void
  activeClassName: string | null
  setActiveClassName: (name: string | null) => void
  /** Look up a class by its hotkey; sets it active and, if a shape is
   * selected, re-classifies that shape too (UX-1). */
  applyHotkeyClass: (hotkey: string) => void
  canUndo: boolean
  canRedo: boolean
  undo: () => void
  redo: () => void
  deleteSelected: () => void
  // Pointer-driven drawing lifecycle (image-space points throughout).
  beginBBoxDrag: (point: Point2D) => void
  updateBBoxDrag: (point: Point2D) => void
  commitBBoxDrag: () => void
  addPolygonPoint: (point: Point2D) => void
  closePolygon: () => void
  addPolylinePoint: (point: Point2D) => void
  finishPolyline: () => void
  /** Rotated box, three clicks: side start, side end, then the far side. */
  addRBoxPoint: (point: Point2D) => void
  updateRBoxPreview: (point: Point2D) => void
  cancelDraft: () => void
  /** Drops the last clicked point of a polygon / polyline / rbox / keypoints draft. */
  removeLastPolygonPoint: () => void
  /** Brush / eraser (masks): one stroke is one undo step. The radius is in
   * image pixels. A paint stroke grows the selected mask, or starts a new one
   * in the active class and selects it; an erase stroke shrinks the selected
   * mask and removes it once empty. */
  beginStroke: (point: Point2D, radius: number, erase: boolean) => void
  /** Keypoints: places the next point of the skeleton (visible, or occluded),
   * starting one in the active class — or the first class with a skeleton —
   * when none is in progress; the last point commits the shape. */
  addKeypoint: (point: Point2D, occluded: boolean) => void
  /** The class the keypoints tool places points for: the draft's, else the
   * one a new skeleton would start in. */
  keypointsClass: LabelClass | null
  /** Marks the next point as not labelled (v = 0). */
  skipKeypoint: () => void
  /** Commits with the remaining points not labelled; nothing when none is. */
  finishKeypoints: () => void
  extendStroke: (point: Point2D) => void
  commitStroke: () => void
  addPointShape: (point: Point2D) => void
  // Smart polygon (ML-7): the gesture is collected here, the model call is the
  // caller's; the answer comes back through `addPolygonShape`.
  beginSmartDrag: (point: Point2D) => void
  updateSmartDrag: (point: Point2D) => void
  /** Ends the gesture: a box when it was dragged, a point when it was a click. */
  finishSmartDrag: () => SmartPrompt | null
  /** Adds a ready polygon (e.g. from a model) as if drawn by hand, in the
   * active class; fewer than three usable points adds nothing. */
  addPolygonShape: (points: Point2D[]) => void
  /** Post-draw editing: replaces the shape with the same id, one undo step.
   * The caller previews a move / handle drag and commits it on release. */
  updateShape: (next: Shape) => void
  /** Paints into the selected mask (or a new one in the active class, then
   * selected) with `paint`, or erases from the selected mask when `erase`;
   * one undo step. The brush and the superpixel tool both end here. */
  paintMask: (erase: boolean, paint: (bitmap: Bitmap, value: 0 | 1) => void) => void
}

export function useDrawing(options: UseDrawingOptions): UseDrawingResult {
  const { value, onChange, classes, imageWidth, imageHeight, readOnly = false } = options

  const [history, setHistory] = useState<HistoryState<AnnotationResult>>(() => createHistory(value))
  const historyRef = useRef(history)
  historyRef.current = history

  // Resync when the parent hands us a genuinely different value, but never on
  // the round-trip of our own commit (same reference). An external edit (the
  // attribute editor in the page sidebar, TOOL-2) is pushed as one undoable
  // step; a *new item* must remount the hook (`key={itemId}` on the annotator)
  // so its history starts empty rather than undoing into the previous item.
  useEffect(() => {
    if (value !== historyRef.current.present) {
      setHistory((h) => pushHistory(h, value))
    }
  }, [value])

  const [tool, setToolState] = useState<Tool>('select')
  const [draft, setDraftState] = useState<Draft>(null)

  // The draft is mirrored into a ref so a commit can read it directly. React
  // (StrictMode) may invoke a state updater twice, so a commit inside
  // `setDraft((d) => …)` would add the shape twice; every commit below reads
  // `draftRef` and calls `commit` exactly once, outside any updater.
  const draftRef = useRef<Draft>(null)
  const setDraft = useCallback((next: Draft) => {
    draftRef.current = next
    setDraftState(next)
  }, [])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [activeClassName, setActiveClassName] = useState<string | null>(classes[0]?.name ?? null)

  // Selection is mirrored into a ref for the same reason history is: an action
  // dispatched in the same tick as select() would otherwise close over the
  // previous value and act on the wrong shape (or on nothing at all).
  const selectedIdRef = useRef<string | null>(selectedId)
  const setSelection = useCallback((id: string | null) => {
    selectedIdRef.current = id
    setSelectedId(id)
  }, [])

  const commit = useCallback(
    (next: AnnotationResult) => {
      setHistory((h) => pushHistory(h, next))
      onChange(next)
    },
    [onChange],
  )

  const setTool = useCallback(
    (next: Tool) => {
      setDraft(null)
      setToolState(next)
    },
    [setDraft],
  )

  const select = useCallback(
    (id: string | null) => {
      setSelection(id)
    },
    [setSelection],
  )

  const applyHotkeyClass = useCallback(
    (hotkey: string) => {
      const cls = classes.find((c) => c.hotkey === hotkey)
      if (!cls) return
      setActiveClassName(cls.name)
      const target = selectedIdRef.current
      if (readOnly || !target) return
      const current = historyRef.current.present
      const shapes = current.shapes.map((s) =>
        s.id === target
          ? { ...s, class: cls.name, attributes: reconcileAttributes(s.attributes, cls) }
          : s,
      )
      if (shapes !== current.shapes) commit({ ...current, shapes })
    },
    [classes, commit, readOnly],
  )

  // Step through history from the ref, outside any state updater: calling the
  // parent's onChange inside `setHistory((h) => …)` would set its state while
  // this component renders (and twice under StrictMode).
  const step = useCallback(
    (move: (h: HistoryState<AnnotationResult>) => HistoryState<AnnotationResult>) => {
      if (readOnly) return
      const current = historyRef.current
      const next = move(current)
      if (next === current) return
      historyRef.current = next
      setHistory(next)
      onChange(next.present)
    },
    [onChange, readOnly],
  )
  const undo = useCallback(() => step(undoHistory), [step])
  const redo = useCallback(() => step(redoHistory), [step])

  const deleteSelected = useCallback(() => {
    const target = selectedIdRef.current
    if (readOnly || !target) return
    const current = historyRef.current.present
    const shapes = current.shapes.filter((s) => s.id !== target)
    if (shapes.length === current.shapes.length) return
    commit({ ...current, shapes })
    setSelection(null)
  }, [commit, readOnly, setSelection])

  /** Common fields of a freshly drawn shape: id, active class, defaults. */
  const newShapeBase = useCallback(() => {
    const cls = classes.find((c) => c.name === activeClassName) ?? classes[0]
    return {
      id: newShapeId(),
      class: activeClassName ?? classes[0]?.name ?? '',
      attributes: defaultAttributes(cls),
      confidence: null,
    }
  }, [activeClassName, classes])

  // --- bbox drawing -----------------------------------------------------

  const beginBBoxDrag = useCallback(
    (point: Point2D) => {
      if (readOnly) return
      const clamped = clampPointToImage(point, imageWidth, imageHeight)
      setDraft({ tool: 'bbox', start: clamped, current: clamped })
    },
    [imageHeight, imageWidth, readOnly, setDraft],
  )

  const updateBBoxDrag = useCallback(
    (point: Point2D) => {
      const d = draftRef.current
      if (!d || d.tool !== 'bbox') return
      setDraft({ ...d, current: clampPointToImage(point, imageWidth, imageHeight) })
    },
    [imageHeight, imageWidth, setDraft],
  )

  const commitBBoxDrag = useCallback(() => {
    const d = draftRef.current
    if (!d || d.tool !== 'bbox') return
    setDraft(null)
    const bbox = clampBBoxToImage(normalizeBBox(d.start, d.current), imageWidth, imageHeight)
    if (isDegenerateBBox(bbox, MIN_BBOX_SIZE)) return
    const current = historyRef.current.present
    const shape: Shape = { ...newShapeBase(), type: 'bbox', bbox }
    commit({ ...current, shapes: [...current.shapes, shape] })
    setSelection(shape.id)
  }, [commit, imageHeight, imageWidth, newShapeBase, setDraft, setSelection])

  // --- polygon drawing ----------------------------------------------------

  const addPolygonPoint = useCallback(
    (point: Point2D) => {
      if (readOnly) return
      const clamped = clampPointToImage(point, imageWidth, imageHeight)
      const d = draftRef.current
      if (!d || d.tool !== 'polygon') setDraft({ tool: 'polygon', points: [clamped] })
      else setDraft({ ...d, points: [...d.points, clamped] })
    },
    [imageHeight, imageWidth, readOnly, setDraft],
  )

  const closePolygon = useCallback(() => {
    const d = draftRef.current
    if (!d || d.tool !== 'polygon') return
    const points = trimTrailingDuplicates(d.points)
    if (points.length < MIN_POLYGON_POINTS) return
    setDraft(null)
    const current = historyRef.current.present
    const shape: Shape = { ...newShapeBase(), type: 'polygon', points }
    commit({ ...current, shapes: [...current.shapes, shape] })
    setSelection(shape.id)
  }, [commit, newShapeBase, setDraft, setSelection])

  // --- polyline drawing ---------------------------------------------------

  const addPolylinePoint = useCallback(
    (point: Point2D) => {
      if (readOnly) return
      const clamped = clampPointToImage(point, imageWidth, imageHeight)
      const d = draftRef.current
      if (!d || d.tool !== 'polyline') setDraft({ tool: 'polyline', points: [clamped] })
      else setDraft({ ...d, points: [...d.points, clamped] })
    },
    [imageHeight, imageWidth, readOnly, setDraft],
  )

  const finishPolyline = useCallback(() => {
    const d = draftRef.current
    if (!d || d.tool !== 'polyline') return
    const points = trimTrailingDuplicates(d.points)
    if (points.length < MIN_POLYLINE_POINTS) return
    setDraft(null)
    const current = historyRef.current.present
    const shape: Shape = { ...newShapeBase(), type: 'polyline', points }
    commit({ ...current, shapes: [...current.shapes, shape] })
    setSelection(shape.id)
  }, [commit, newShapeBase, setDraft, setSelection])

  // --- rotated box drawing ------------------------------------------------

  const addRBoxPoint = useCallback(
    (point: Point2D) => {
      if (readOnly) return
      const clamped = clampPointToImage(point, imageWidth, imageHeight)
      const d = draftRef.current
      if (!d || d.tool !== 'rbox') {
        setDraft({ tool: 'rbox', points: [clamped], current: clamped })
        return
      }
      if (d.points.length === 1) {
        // Second click ends the first side; a click on top of the first is ignored.
        if (d.points[0][0] === clamped[0] && d.points[0][1] === clamped[1]) return
        setDraft({ ...d, points: [d.points[0], clamped], current: clamped })
        return
      }
      // Third click fixes the far side and commits.
      const geometry = rboxFromThreePoints(d.points[0], d.points[1], clamped)
      if (!geometry || geometry.size[0] < MIN_BBOX_SIZE || geometry.size[1] < MIN_BBOX_SIZE) {
        return
      }
      setDraft(null)
      const current = historyRef.current.present
      const shape: Shape = { ...newShapeBase(), type: 'rbox', ...geometry }
      commit({ ...current, shapes: [...current.shapes, shape] })
      setSelection(shape.id)
    },
    [commit, imageHeight, imageWidth, newShapeBase, readOnly, setDraft, setSelection],
  )

  const updateRBoxPreview = useCallback(
    (point: Point2D) => {
      const d = draftRef.current
      if (!d || d.tool !== 'rbox') return
      setDraft({ ...d, current: clampPointToImage(point, imageWidth, imageHeight) })
    },
    [imageHeight, imageWidth, setDraft],
  )

  const cancelDraft = useCallback(() => {
    setDraft(null)
  }, [setDraft])

  const removeLastPolygonPoint = useCallback(() => {
    const d = draftRef.current
    if (!d || d.tool === 'bbox' || d.tool === 'smart' || d.tool === 'brush') return
    if (d.points.length <= 1) {
      setDraft(null)
    } else if (d.tool === 'keypoints') {
      // Its own branch so each draft keeps its own point type.
      setDraft({ ...d, points: d.points.slice(0, -1) })
    } else {
      setDraft({ ...d, points: d.points.slice(0, -1) })
    }
  }, [setDraft])

  // --- point tool -----------------------------------------------------------

  const addPointShape = useCallback(
    (point: Point2D) => {
      if (readOnly) return
      const clamped = clampPointToImage(point, imageWidth, imageHeight)
      const current = historyRef.current.present
      const shape: Shape = { ...newShapeBase(), type: 'point', point: clamped }
      commit({ ...current, shapes: [...current.shapes, shape] })
      setSelection(shape.id)
    },
    [commit, imageHeight, imageWidth, newShapeBase, readOnly, setSelection],
  )

  // --- brush / eraser (masks) -------------------------------------------

  const beginStroke = useCallback(
    (point: Point2D, radius: number, erase: boolean) => {
      if (readOnly || !canBrush(imageWidth, imageHeight)) return
      const clamped = clampPointToImage(point, imageWidth, imageHeight)
      setDraft({ tool: 'brush', erase, points: [clamped], radius })
    },
    [imageHeight, imageWidth, readOnly, setDraft],
  )

  const extendStroke = useCallback(
    (point: Point2D) => {
      const d = draftRef.current
      if (!d || d.tool !== 'brush') return
      const clamped = clampPointToImage(point, imageWidth, imageHeight)
      setDraft({ ...d, points: [...d.points, clamped] })
    },
    [imageHeight, imageWidth, setDraft],
  )

  const paintMask = useCallback(
    (erase: boolean, paint: (bitmap: Bitmap, value: 0 | 1) => void) => {
      if (readOnly) return
      const current = historyRef.current.present
      const selected = current.shapes.find((s) => s.id === selectedIdRef.current)
      const target =
        selected?.type === 'mask' &&
        selected.rle.size[0] === imageHeight &&
        selected.rle.size[1] === imageWidth
          ? selected
          : null
      if (erase && !target) return
      const bitmap = (target && decodeRle(target.rle)) ?? emptyBitmap(imageWidth, imageHeight)
      paint(bitmap, erase ? 0 : 1)
      const rle = encodeRle(bitmap)
      if (target) {
        const shapes = isEmptyRle(rle)
          ? current.shapes.filter((s) => s.id !== target.id)
          : current.shapes.map((s) => (s.id === target.id ? { ...target, rle } : s))
        if (isEmptyRle(rle)) setSelection(null)
        commit({ ...current, shapes })
        return
      }
      if (isEmptyRle(rle)) return
      const shape: Shape = { ...newShapeBase(), type: 'mask', rle }
      commit({ ...current, shapes: [...current.shapes, shape] })
      setSelection(shape.id)
    },
    [commit, imageHeight, imageWidth, newShapeBase, readOnly, setSelection],
  )

  const commitStroke = useCallback(() => {
    const d = draftRef.current
    if (!d || d.tool !== 'brush') return
    setDraft(null)
    paintMask(d.erase, (bitmap, value) => paintStroke(bitmap, d.points, d.radius, value))
  }, [paintMask, setDraft])

  // --- keypoint skeletons ------------------------------------------------

  const skeletonClass = useCallback((): LabelClass | null => {
    const active = classes.find((c) => c.name === activeClassName)
    if (active?.skeleton && active.tools.includes('keypoints')) return active
    return classes.find((c) => c.skeleton && c.tools.includes('keypoints')) ?? null
  }, [activeClassName, classes])

  /** Commits `points` (padded to the skeleton) as a new shape in `cls`. */
  const commitKeypoints = useCallback(
    (cls: LabelClass, placed: Keypoint[]) => {
      setDraft(null)
      const size = cls.skeleton?.points.length ?? 0
      const points: Keypoint[] = [...placed]
      while (points.length < size) points.push([0, 0, 0])
      if (!points.some(([, , v]) => v > 0)) return
      const current = historyRef.current.present
      const shape: Shape = {
        ...newShapeBase(),
        class: cls.name,
        attributes: defaultAttributes(cls),
        type: 'keypoints',
        points,
      }
      commit({ ...current, shapes: [...current.shapes, shape] })
      setSelection(shape.id)
    },
    [commit, newShapeBase, setDraft, setSelection],
  )

  /** Appends one point to the draft (starting one if needed), committing
   * when the skeleton is complete. */
  const pushKeypoint = useCallback(
    (keypoint: Keypoint) => {
      if (readOnly) return
      const d = draftRef.current
      const cls =
        d?.tool === 'keypoints' ? classes.find((c) => c.name === d.className) : skeletonClass()
      if (!cls?.skeleton) return
      const points = [...(d?.tool === 'keypoints' ? d.points : []), keypoint]
      if (points.length >= cls.skeleton.points.length) commitKeypoints(cls, points)
      else setDraft({ tool: 'keypoints', className: cls.name, points })
    },
    [classes, commitKeypoints, readOnly, setDraft, skeletonClass],
  )

  const addKeypoint = useCallback(
    (point: Point2D, occluded: boolean) => {
      const [x, y] = clampPointToImage(point, imageWidth, imageHeight)
      pushKeypoint([x, y, occluded ? 1 : 2])
    },
    [imageHeight, imageWidth, pushKeypoint],
  )

  const skipKeypoint = useCallback(() => pushKeypoint([0, 0, 0]), [pushKeypoint])

  const finishKeypoints = useCallback(() => {
    const d = draftRef.current
    if (!d || d.tool !== 'keypoints') return
    const cls = classes.find((c) => c.name === d.className)
    if (cls) commitKeypoints(cls, d.points)
    else setDraft(null)
  }, [classes, commitKeypoints, setDraft])

  // --- smart polygon (ML-7) ---------------------------------------------

  const beginSmartDrag = useCallback(
    (point: Point2D) => {
      if (readOnly) return
      const clamped = clampPointToImage(point, imageWidth, imageHeight)
      setDraft({ tool: 'smart', start: clamped, current: clamped })
    },
    [imageHeight, imageWidth, readOnly, setDraft],
  )

  const updateSmartDrag = useCallback(
    (point: Point2D) => {
      const d = draftRef.current
      if (!d || d.tool !== 'smart') return
      setDraft({ ...d, current: clampPointToImage(point, imageWidth, imageHeight) })
    },
    [imageHeight, imageWidth, setDraft],
  )

  const finishSmartDrag = useCallback((): SmartPrompt | null => {
    const d = draftRef.current
    if (!d || d.tool !== 'smart') return null
    setDraft(null)
    const bbox = clampBBoxToImage(normalizeBBox(d.start, d.current), imageWidth, imageHeight)
    if (isDegenerateBBox(bbox, MIN_BBOX_SIZE)) return { kind: 'point', point: d.start }
    return { kind: 'box', bbox }
  }, [imageHeight, imageWidth, setDraft])

  const addPolygonShape = useCallback(
    (points: Point2D[]) => {
      if (readOnly) return
      const clamped = trimTrailingDuplicates(
        points.map((p) => clampPointToImage(p, imageWidth, imageHeight)),
      )
      if (clamped.length < MIN_POLYGON_POINTS) return
      const current = historyRef.current.present
      const shape: Shape = { ...newShapeBase(), type: 'polygon', points: clamped }
      commit({ ...current, shapes: [...current.shapes, shape] })
      setSelection(shape.id)
    },
    [commit, imageHeight, imageWidth, newShapeBase, readOnly, setSelection],
  )

  // --- select-tool editing -----------------------------------------------

  const updateShape = useCallback(
    (next: Shape) => {
      if (readOnly) return
      const current = historyRef.current.present
      const index = current.shapes.findIndex((s) => s.id === next.id)
      if (index < 0 || current.shapes[index] === next) return
      commit({ ...current, shapes: current.shapes.map((s, i) => (i === index ? next : s)) })
    },
    [commit, readOnly],
  )

  return {
    tool,
    setTool,
    draft,
    selectedId,
    select,
    activeClassName,
    setActiveClassName,
    applyHotkeyClass,
    canUndo: history.past.length > 0,
    canRedo: history.future.length > 0,
    undo,
    redo,
    deleteSelected,
    beginBBoxDrag,
    updateBBoxDrag,
    commitBBoxDrag,
    addPolygonPoint,
    closePolygon,
    addPolylinePoint,
    finishPolyline,
    addRBoxPoint,
    updateRBoxPreview,
    cancelDraft,
    removeLastPolygonPoint,
    beginStroke,
    extendStroke,
    commitStroke,
    addKeypoint,
    skipKeypoint,
    finishKeypoints,
    keypointsClass:
      draft?.tool === 'keypoints'
        ? (classes.find((c) => c.name === draft.className) ?? null)
        : skeletonClass(),
    addPointShape,
    beginSmartDrag,
    updateSmartDrag,
    finishSmartDrag,
    addPolygonShape,
    updateShape,
    paintMask,
  }
}
