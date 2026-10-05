/**
 * Post-draw shape editing (TOOL): moving a whole shape and dragging its
 * handles. Pure functions over wire shapes in image pixels, so the gesture in
 * the annotator only previews their result and commits it once on release.
 *
 * Handles per shape type:
 * - bbox: corners 0-3 (nw, ne, se, sw), then edge midpoints 4-7 (n, e, s, w)
 * - rbox: corners 0-3 in `rboxCorners` order, then 4 = rotation
 * - polygon / polyline: one `vertex` per point, then one `insert` per edge
 *   (dragging it adds a vertex there)
 * - keypoints: one `vertex` per labelled point, `index` = skeleton slot
 * - point: none, the shape itself is the handle
 * - mask: none, and not movable; the brush edits it
 */

import {
  boundingBoxOfShape,
  clampPointToImage,
  isDegenerateBBox,
  normalizeBBox,
  rboxCorners,
} from './geometry'
import type { BBox, Keypoint, Point2D, Shape } from './types'

/** Smallest box side a handle drag may leave, in image pixels. */
export const MIN_EDIT_SIZE = 3
/** Screen pixels between an rbox's top edge and its rotation handle. */
export const ROTATE_HANDLE_OFFSET_PX = 24

const MIN_POLYGON_POINTS = 3
const MIN_POLYLINE_POINTS = 2

export type HandleKind = 'vertex' | 'insert' | 'rotate'

export interface Handle {
  /** Stable id of the handle within its shape, passed back to `dragHandle`. */
  index: number
  kind: HandleKind
  point: Point2D
}

const MOVABLE = new Set<Shape['type']>([
  'bbox',
  'rbox',
  'polygon',
  'polyline',
  'point',
  'keypoints',
])

export function isMovable(shape: Shape): boolean {
  return MOVABLE.has(shape.type)
}

/** The handles of `shape`; `scale` places the rbox rotation handle a fixed
 * screen distance from the box. */
export function shapeHandles(shape: Shape, scale: number): Handle[] {
  switch (shape.type) {
    case 'bbox': {
      const [x0, y0, x1, y1] = shape.bbox
      const mx = (x0 + x1) / 2
      const my = (y0 + y1) / 2
      const points: Point2D[] = [
        [x0, y0],
        [x1, y0],
        [x1, y1],
        [x0, y1],
        [mx, y0],
        [x1, my],
        [mx, y1],
        [x0, my],
      ]
      return points.map((point, index) => ({ index, kind: 'vertex', point }))
    }
    case 'rbox': {
      const corners = rboxCorners(shape.center, shape.size, shape.angle)
      const handles: Handle[] = corners.map((point, index) => ({ index, kind: 'vertex', point }))
      handles.push({ index: 4, kind: 'rotate', point: rotateHandlePoint(shape, scale) })
      return handles
    }
    case 'polygon':
    case 'polyline': {
      const { points } = shape
      const handles: Handle[] = points.map((point, index) => ({ index, kind: 'vertex', point }))
      const edges = shape.type === 'polygon' ? points.length : points.length - 1
      for (let i = 0; i < edges; i += 1) {
        const [ax, ay] = points[i]
        const [bx, by] = points[(i + 1) % points.length]
        const point: Point2D = [(ax + bx) / 2, (ay + by) / 2]
        handles.push({ index: points.length + i, kind: 'insert', point })
      }
      return handles
    }
    case 'keypoints':
      return shape.points.flatMap(([x, y, v], index) =>
        v === 0 ? [] : [{ index, kind: 'vertex' as const, point: [x, y] as Point2D }],
      )
    default:
      return []
  }
}

function rotateHandlePoint(shape: Extract<Shape, { type: 'rbox' }>, scale: number): Point2D {
  const theta = (shape.angle * Math.PI) / 180
  const reach = shape.size[1] / 2 + ROTATE_HANDLE_OFFSET_PX / scale
  // Local (0, -reach), rotated like `rboxCorners` does.
  return [shape.center[0] + reach * Math.sin(theta), shape.center[1] - reach * Math.cos(theta)]
}

/** `shape` shifted by (dx, dy), the shift reduced so its bounds stay inside
 * the image; null for a shape that cannot be moved (a mask). */
export function translateShape(
  shape: Shape,
  dx: number,
  dy: number,
  imageWidth: number,
  imageHeight: number,
): Shape | null {
  if (!isMovable(shape)) return null
  const bounds = boundingBoxOfShape(shape)
  if (bounds) {
    dx = clampShift(dx, bounds[0], bounds[2], imageWidth)
    dy = clampShift(dy, bounds[1], bounds[3], imageHeight)
  }
  const move = ([x, y]: Point2D): Point2D => [x + dx, y + dy]
  switch (shape.type) {
    case 'bbox': {
      const [x0, y0, x1, y1] = shape.bbox
      return { ...shape, bbox: [x0 + dx, y0 + dy, x1 + dx, y1 + dy] }
    }
    case 'rbox':
      return { ...shape, center: move(shape.center) }
    case 'polygon':
    case 'polyline':
      return { ...shape, points: shape.points.map(move) }
    case 'point':
      return { ...shape, point: move(shape.point) }
    case 'keypoints':
      // Unlabelled slots keep their [0, 0, 0] placeholder.
      return {
        ...shape,
        points: shape.points.map(([x, y, v]) => (v === 0 ? [x, y, v] : [x + dx, y + dy, v])),
      }
    default:
      return null
  }
}

/** A shift of the span [min, max] limited so it stays within [0, limit]. When
 * the span is already outside (pre-labels may be), it is not pushed further. */
function clampShift(shift: number, min: number, max: number, limit: number): number {
  const low = Math.min(0, -min)
  const high = Math.max(0, limit - max)
  return Math.min(high, Math.max(low, shift))
}

/** `shape` with handle `index` dragged to `point` (image pixels, clamped to
 * the image); null when the result would be degenerate or the handle does
 * not exist, so the caller keeps its previous preview. */
export function dragHandle(
  shape: Shape,
  index: number,
  point: Point2D,
  imageWidth: number,
  imageHeight: number,
): Shape | null {
  const p = clampPointToImage(point, imageWidth, imageHeight)
  switch (shape.type) {
    case 'bbox': {
      const bbox = dragBBoxHandle(shape.bbox, index, p)
      return bbox && !isDegenerateBBox(bbox, MIN_EDIT_SIZE) ? { ...shape, bbox } : null
    }
    case 'rbox':
      return index === 4 ? rotateRBox(shape, p) : resizeRBox(shape, index, p)
    case 'polygon':
    case 'polyline': {
      const n = shape.points.length
      if (index < n) {
        return { ...shape, points: shape.points.map((q, i) => (i === index ? p : q)) }
      }
      const edge = index - n
      const edges = shape.type === 'polygon' ? n : n - 1
      if (edge >= edges) return null
      const points = [...shape.points]
      points.splice(edge + 1, 0, p)
      return { ...shape, points }
    }
    case 'keypoints': {
      const slot = shape.points[index]
      if (!slot || slot[2] === 0) return null
      return {
        ...shape,
        points: shape.points.map((q, i) => (i === index ? [p[0], p[1], q[2]] : q)),
      }
    }
    default:
      return null
  }
}

function dragBBoxHandle(bbox: BBox, index: number, p: Point2D): BBox | null {
  const [x0, y0, x1, y1] = bbox
  switch (index) {
    case 0:
      return normalizeBBox([x1, y1], p)
    case 1:
      return normalizeBBox([x0, y1], p)
    case 2:
      return normalizeBBox([x0, y0], p)
    case 3:
      return normalizeBBox([x1, y0], p)
    case 4:
      return normalizeBBox([x0, p[1]], [x1, y1])
    case 5:
      return normalizeBBox([x0, y0], [p[0], y1])
    case 6:
      return normalizeBBox([x0, y0], [x1, p[1]])
    case 7:
      return normalizeBBox([p[0], y0], [x1, y1])
    default:
      return null
  }
}

/** Corner drag: the opposite corner stays put, the angle is kept. */
function resizeRBox(
  shape: Extract<Shape, { type: 'rbox' }>,
  index: number,
  p: Point2D,
): Shape | null {
  if (index < 0 || index > 3) return null
  const opposite = rboxCorners(shape.center, shape.size, shape.angle)[(index + 2) % 4]
  const theta = (shape.angle * Math.PI) / 180
  const dx = p[0] - opposite[0]
  const dy = p[1] - opposite[1]
  const width = Math.abs(dx * Math.cos(theta) + dy * Math.sin(theta))
  const height = Math.abs(-dx * Math.sin(theta) + dy * Math.cos(theta))
  if (width < MIN_EDIT_SIZE || height < MIN_EDIT_SIZE) return null
  return {
    ...shape,
    center: [(opposite[0] + p[0]) / 2, (opposite[1] + p[1]) / 2],
    size: [width, height],
  }
}

/** Rotation: the handle points from the centre at the pointer. Degrees in
 * (-180, 180], clockwise-positive (docs/CONTRACTS.md). */
function rotateRBox(shape: Extract<Shape, { type: 'rbox' }>, p: Point2D): Shape | null {
  const dx = p[0] - shape.center[0]
  const dy = p[1] - shape.center[1]
  if (dx === 0 && dy === 0) return null
  let angle = (Math.atan2(dy, dx) * 180) / Math.PI + 90
  if (angle > 180) angle -= 360
  return { ...shape, angle }
}

/** `shape` without vertex `index` (Alt+click on a polygon, polyline or
 * keypoint handle); null when that would leave too few points. A keypoint is
 * marked not labelled rather than removed: its slot is its identity. */
export function removeVertex(shape: Shape, index: number): Shape | null {
  switch (shape.type) {
    case 'polygon':
    case 'polyline': {
      const min = shape.type === 'polygon' ? MIN_POLYGON_POINTS : MIN_POLYLINE_POINTS
      if (index >= shape.points.length || shape.points.length <= min) return null
      return { ...shape, points: shape.points.filter((_, i) => i !== index) }
    }
    case 'keypoints': {
      const labelled = shape.points.filter(([, , v]) => v > 0).length
      if (!shape.points[index] || shape.points[index][2] === 0 || labelled <= 1) return null
      const unlabelled: Keypoint = [0, 0, 0]
      return { ...shape, points: shape.points.map((q, i) => (i === index ? unlabelled : q)) }
    }
    default:
      return null
  }
}
