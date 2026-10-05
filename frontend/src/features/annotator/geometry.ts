/**
 * Pure coordinate-conversion, clamping and hit-testing helpers.
 *
 * Coordinate discipline (DATA-8): everything in `AnnotationResult` lives in
 * ORIGINAL IMAGE PIXELS, origin top-left, x right, y down — never normalised,
 * never in stage/screen space. This module is the *only* place that boundary
 * is crossed. A "stage point" below means a raw pointer position relative to
 * the Konva Stage's DOM container (i.e. before the Stage's own scale/position
 * transform is applied) — see useViewport.ts for how the Stage transform is
 * wired up so that Konva's native dragging reports positions in image space.
 */

import type { BBox, Keypoint, Point2D, Shape } from './types'
import { rleBounds } from './mask'

export interface Viewport {
  scale: number
  offsetX: number
  offsetY: number
}

export const MIN_SCALE = 0.02
export const MAX_SCALE = 40

export function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

export function clampScale(scale: number): number {
  if (!Number.isFinite(scale)) return MIN_SCALE
  return clamp(scale, MIN_SCALE, MAX_SCALE)
}

/** Convert a point in original-image pixel space to stage (screen) space. */
export function toStageCoords(point: Point2D, viewport: Viewport): Point2D {
  return [point[0] * viewport.scale + viewport.offsetX, point[1] * viewport.scale + viewport.offsetY]
}

/** Convert a point in stage (screen) space back to original-image pixel space. */
export function toImageCoords(point: Point2D, viewport: Viewport): Point2D {
  return [(point[0] - viewport.offsetX) / viewport.scale, (point[1] - viewport.offsetY) / viewport.scale]
}

export function clampPointToImage(point: Point2D, imageWidth: number, imageHeight: number): Point2D {
  return [clamp(point[0], 0, imageWidth), clamp(point[1], 0, imageHeight)]
}

/** Normalise two opposite drag corners into [xMin, yMin, xMax, yMax], regardless
 * of which direction the user dragged (right-to-left, bottom-to-top, etc). */
export function normalizeBBox(a: Point2D, b: Point2D): BBox {
  const xMin = Math.min(a[0], b[0])
  const yMin = Math.min(a[1], b[1])
  const xMax = Math.max(a[0], b[0])
  const yMax = Math.max(a[1], b[1])
  return [xMin, yMin, xMax, yMax]
}

export function clampBBoxToImage(bbox: BBox, imageWidth: number, imageHeight: number): BBox {
  const [xMin, yMin, xMax, yMax] = bbox
  return [
    clamp(xMin, 0, imageWidth),
    clamp(yMin, 0, imageHeight),
    clamp(xMax, 0, imageWidth),
    clamp(yMax, 0, imageHeight),
  ]
}

/** A box narrower or shorter than `minSize` image pixels is not a deliberate
 * annotation — reject it (e.g. an accidental click-without-drag). */
export function isDegenerateBBox(bbox: BBox, minSize = 3): boolean {
  const width = bbox[2] - bbox[0]
  const height = bbox[3] - bbox[1]
  return width < minSize || height < minSize
}

/** Shoelace formula. Points need not be closed (first point not repeated). */
export function polygonArea(points: Point2D[]): number {
  if (points.length < 3) return 0
  let sum = 0
  for (let i = 0; i < points.length; i++) {
    const [x1, y1] = points[i]
    const [x2, y2] = points[(i + 1) % points.length]
    sum += x1 * y2 - x2 * y1
  }
  return Math.abs(sum) / 2
}

/** Ray-casting point-in-polygon test. Points need not be closed. */
export function pointInPolygon(point: Point2D, points: Point2D[]): boolean {
  const [px, py] = point
  let inside = false
  for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
    const [xi, yi] = points[i]
    const [xj, yj] = points[j]
    const intersects = yi > py !== yj > py && px < ((xj - xi) * (py - yi)) / (yj - yi) + xi
    if (intersects) inside = !inside
  }
  return inside
}

export function distance(a: Point2D, b: Point2D): number {
  return Math.hypot(a[0] - b[0], a[1] - b[1])
}

/** The four corners of a rotated box, clockwise from its own top-left. */
export function rboxCorners(center: Point2D, size: [number, number], angle: number): Point2D[] {
  const [cx, cy] = center
  const [halfW, halfH] = [size[0] / 2, size[1] / 2]
  const theta = (angle * Math.PI) / 180
  const cos = Math.cos(theta)
  const sin = Math.sin(theta)
  const offsets: Point2D[] = [
    [-halfW, -halfH],
    [halfW, -halfH],
    [halfW, halfH],
    [-halfW, halfH],
  ]
  return offsets.map(([dx, dy]) => [cx + dx * cos - dy * sin, cy + dx * sin + dy * cos])
}

export interface RBoxGeometry {
  center: Point2D
  size: [number, number]
  angle: number
}

/** Rotated box from the three-click gesture: `a`→`b` is one full side (it
 * sets the width and the angle) and `c` sets how far the opposite side lies,
 * measured perpendicular to a→b. Null while a→b has no length. */
export function rboxFromThreePoints(a: Point2D, b: Point2D, c: Point2D): RBoxGeometry | null {
  const width = distance(a, b)
  if (width === 0) return null
  const ux = (b[0] - a[0]) / width
  const uy = (b[1] - a[1]) / width
  // Unit normal, rotated +90° (clockwise on screen) from the side direction.
  const nx = -uy
  const ny = ux
  const depth = (c[0] - a[0]) * nx + (c[1] - a[1]) * ny
  const center: Point2D = [
    a[0] + (ux * width) / 2 + (nx * depth) / 2,
    a[1] + (uy * width) / 2 + (ny * depth) / 2,
  ]
  let angle = (Math.atan2(uy, ux) * 180) / Math.PI
  if (angle <= -180) angle += 360
  return { center, size: [width, Math.abs(depth)], angle }
}

export interface ImageRect {
  x: number
  y: number
  width: number
  height: number
}

/** The image-space rectangle currently visible on the stage — used for
 * virtualisation (IMG-4): only shapes overlapping this rect need to render. */
export function getVisibleImageRect(viewport: Viewport, stageWidth: number, stageHeight: number): ImageRect {
  const topLeft = toImageCoords([0, 0], viewport)
  const bottomRight = toImageCoords([stageWidth, stageHeight], viewport)
  return {
    x: topLeft[0],
    y: topLeft[1],
    width: bottomRight[0] - topLeft[0],
    height: bottomRight[1] - topLeft[1],
  }
}

function boundsOfPoints(points: Point2D[]): BBox {
  let xMin = Infinity
  let yMin = Infinity
  let xMax = -Infinity
  let yMax = -Infinity
  for (const [x, y] of points) {
    xMin = Math.min(xMin, x)
    yMin = Math.min(yMin, y)
    xMax = Math.max(xMax, x)
    yMax = Math.max(yMax, y)
  }
  return [xMin, yMin, xMax, yMax]
}

/** The `[x, y]` of every labelled (v > 0) keypoint. */
export function labelledKeypoints(points: Keypoint[]): Point2D[] {
  return points.filter(([, , v]) => v > 0).map(([x, y]) => [x, y])
}

/** Axis-aligned bounding box of a shape in image space, or null when it has
 * none (text spans, relations, an empty mask). */
export function boundingBoxOfShape(shape: Shape): BBox | null {
  switch (shape.type) {
    case 'bbox':
      return shape.bbox
    case 'rbox':
      return boundsOfPoints(rboxCorners(shape.center, shape.size, shape.angle))
    case 'polygon':
    case 'polyline': {
      if (shape.points.length === 0) return null
      let xMin = Infinity
      let yMin = Infinity
      let xMax = -Infinity
      let yMax = -Infinity
      for (const [x, y] of shape.points) {
        xMin = Math.min(xMin, x)
        yMin = Math.min(yMin, y)
        xMax = Math.max(xMax, x)
        yMax = Math.max(yMax, y)
      }
      return [xMin, yMin, xMax, yMax]
    }
    case 'point':
      return [shape.point[0], shape.point[1], shape.point[0], shape.point[1]]
    case 'mask':
      return rleBounds(shape.rle)
    case 'keypoints': {
      const labelled = labelledKeypoints(shape.points)
      return labelled.length > 0 ? boundsOfPoints(labelled) : null
    }
    case 'span':
    case 'relation':
    case 'ranking':
    case 'rating':
    case 'segment':
      return null
  }
}

function rectIntersectsBBox(rect: ImageRect, bbox: BBox): boolean {
  return !(bbox[2] < rect.x || bbox[0] > rect.x + rect.width || bbox[3] < rect.y || bbox[1] > rect.y + rect.height)
}

/** True when `shape` overlaps `rect` (optionally padded). Shapes with no
 * computable bounds are always considered visible — we never cull
 * geometry we can't reason about. */
export function isShapeVisible(shape: Shape, rect: ImageRect, padding = 0): boolean {
  const bbox = boundingBoxOfShape(shape)
  if (!bbox) return true
  const padded: ImageRect = {
    x: rect.x - padding,
    y: rect.y - padding,
    width: rect.width + 2 * padding,
    height: rect.height + 2 * padding,
  }
  return rectIntersectsBBox(padded, bbox)
}

/** The viewport that fits the whole image inside the stage, centered. */
export function computeFitViewport(
  imageWidth: number,
  imageHeight: number,
  stageWidth: number,
  stageHeight: number,
): Viewport {
  if (imageWidth <= 0 || imageHeight <= 0 || stageWidth <= 0 || stageHeight <= 0) {
    return { scale: 1, offsetX: 0, offsetY: 0 }
  }
  // Capped at 1: fitting never magnifies. A small image blown up to fill the
  // stage looks sharp but is interpolated, and annotating interpolated pixels
  // produces coordinates the annotator cannot actually see. Zooming in is an
  // explicit action.
  const scale = clampScale(
    Math.min(stageWidth / imageWidth, stageHeight / imageHeight, 1),
  )
  const offsetX = (stageWidth - imageWidth * scale) / 2
  const offsetY = (stageHeight - imageHeight * scale) / 2
  return { scale, offsetX, offsetY }
}

/** Fit the rectangle `focus` (image pixels) to the stage, e.g. a region
 * task's region (IMG-6): same rule as `computeFitViewport`, then shifted so
 * the rectangle, not the image origin, sits in the stage. */
export function computeFocusViewport(
  focus: BBox,
  stageWidth: number,
  stageHeight: number,
): Viewport {
  const [xMin, yMin, xMax, yMax] = focus
  const fit = computeFitViewport(xMax - xMin, yMax - yMin, stageWidth, stageHeight)
  return {
    scale: fit.scale,
    offsetX: fit.offsetX - xMin * fit.scale,
    offsetY: fit.offsetY - yMin * fit.scale,
  }
}

/** Where a region task decides a shape belongs (IMG-6, CONTRACTS.md *task*):
 * the centre of its axis-aligned envelope, or the point itself; null when the
 * client cannot tell (masks, spans, relations — the server decides). */
export function shapeAnchor(shape: Shape): Point2D | null {
  if (shape.type === 'point') return shape.point
  const box = boundingBoxOfShape(shape)
  if (!box) return null
  return [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2]
}

/** Half-open `[x_min, x_max) x [y_min, y_max)`, like the server. */
export function isAnchorInRegion(shape: Shape, region: BBox): boolean {
  const anchor = shapeAnchor(shape)
  if (!anchor) return true
  const [x, y] = anchor
  return x >= region[0] && x < region[2] && y >= region[1] && y < region[3]
}
