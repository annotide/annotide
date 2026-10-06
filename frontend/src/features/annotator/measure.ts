/**
 * Measurements in physical units (TOOL-8). A pixel's size comes from the
 * item's own `meta.pixel_spacing` (DICOM, GeoTIFF), else the project's
 * `settings.calibration`, else lengths stay in pixels. Spacing may differ
 * along x and y, so lengths are measured per axis, not scaled afterwards.
 */
import { polygonArea, rboxCorners } from './geometry'
import { rleArea } from './mask'
import type { Point2D, Shape } from './types'

export interface PixelScale {
  /** Units per pixel along x and y. */
  x: number
  y: number
  unit: string
  source: 'item' | 'project' | 'session' | 'pixels'
}

export const PIXELS: PixelScale = { x: 1, y: 1, unit: 'px', source: 'pixels' }

function positive(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
}

function unitOf(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 && value.length <= 16 ? value : null
}

/** The scale in force for an item, by precedence: item → project → pixels. */
export function resolveScale(
  itemMeta: Record<string, unknown> | undefined,
  projectSettings: Record<string, unknown> | undefined,
): PixelScale {
  const spacing = itemMeta?.pixel_spacing as Record<string, unknown> | undefined
  if (spacing && positive(spacing.x) && positive(spacing.y) && unitOf(spacing.unit)) {
    return { x: spacing.x, y: spacing.y, unit: spacing.unit as string, source: 'item' }
  }
  const calibration = projectSettings?.calibration as Record<string, unknown> | undefined
  if (calibration && positive(calibration.units_per_pixel) && unitOf(calibration.unit)) {
    const size = calibration.units_per_pixel
    return { x: size, y: size, unit: calibration.unit as string, source: 'project' }
  }
  return PIXELS
}

export function length(a: Point2D, b: Point2D, scale: PixelScale): number {
  return Math.hypot((b[0] - a[0]) * scale.x, (b[1] - a[1]) * scale.y)
}

function pathLength(points: Point2D[], scale: PixelScale, closed: boolean): number {
  let total = 0
  for (let i = 1; i < points.length; i += 1) total += length(points[i - 1], points[i], scale)
  if (closed && points.length > 2) total += length(points[points.length - 1], points[0], scale)
  return total
}

function round(value: number): string {
  const digits = value >= 1000 ? 0 : value >= 10 ? 1 : 2
  return value.toLocaleString(undefined, { maximumFractionDigits: digits })
}

export function formatLength(value: number, scale: PixelScale): string {
  return `${round(value)} ${scale.unit}`
}

export function formatArea(value: number, scale: PixelScale): string {
  return `${round(value)} ${scale.unit}²`
}

/** A short measurement line for the shape list; `null` where nothing is measurable. */
export function describeShape(shape: Shape, scale: PixelScale): string | null {
  switch (shape.type) {
    case 'bbox': {
      const [x1, y1, x2, y2] = shape.bbox
      const width = Math.abs(x2 - x1) * scale.x
      return `${round(width)} × ${formatLength(Math.abs(y2 - y1) * scale.y, scale)}`
    }
    case 'rbox': {
      const [a, b, c] = rboxCorners(shape.center, shape.size, shape.angle)
      return `${round(length(a, b, scale))} × ${formatLength(length(b, c, scale), scale)}`
    }
    case 'polygon': {
      const area = formatArea(polygonArea(shape.points) * scale.x * scale.y, scale)
      return `${area}, perimeter ${formatLength(pathLength(shape.points, scale, true), scale)}`
    }
    case 'polyline':
      return formatLength(pathLength(shape.points, scale, false), scale)
    case 'mask':
      return formatArea(rleArea(shape.rle) * scale.x * scale.y, scale)
    default:
      return null
  }
}
