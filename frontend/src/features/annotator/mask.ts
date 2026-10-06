/**
 * Pixel masks for the brush tool (TOOL, `mask` shapes).
 *
 * The wire format is COCO's uncompressed RLE (docs/CONTRACTS.md): `size` is
 * `[height, width]`, `counts` alternate background / foreground run lengths in
 * **column-major** order, starting with background, and sum to
 * `height × width`. The in-memory bitmap here uses the same column-major
 * layout (`index = x * height + y`) so encoding and decoding are one pass.
 */

import type { BBox, MaskRLE, Point2D } from '@/api/types'

/** Largest image the brush edits: one byte per pixel is held while a stroke
 * is committed. Tiled images (IMG-1) are far larger and are not brushable. */
export const MAX_MASK_PIXELS = 50_000_000

export interface Bitmap {
  width: number
  height: number
  /** 0 / 1 per pixel, column-major. */
  bits: Uint8Array
}

export function emptyBitmap(width: number, height: number): Bitmap {
  return { width, height, bits: new Uint8Array(width * height) }
}

export function canBrush(width: number, height: number): boolean {
  return width > 0 && height > 0 && width * height <= MAX_MASK_PIXELS
}

/** RLE → bitmap; null when the counts do not cover exactly `height × width`. */
export function decodeRle(rle: MaskRLE): Bitmap | null {
  const [height, width] = rle.size
  const total = width * height
  const bits = new Uint8Array(total)
  let index = 0
  for (let i = 0; i < rle.counts.length; i += 1) {
    const run = rle.counts[i]
    if (run < 0 || index + run > total) return null
    if (i % 2 === 1) bits.fill(1, index, index + run)
    index += run
  }
  return index === total ? { width, height, bits } : null
}

export function encodeRle(bitmap: Bitmap): MaskRLE {
  const { width, height, bits } = bitmap
  const counts: number[] = []
  let value = 0
  let run = 0
  for (let i = 0; i < bits.length; i += 1) {
    if (bits[i] !== value) {
      counts.push(run)
      value = bits[i]
      run = 0
    }
    run += 1
  }
  counts.push(run)
  return { size: [height, width], counts }
}

export function isEmptyRle(rle: MaskRLE): boolean {
  for (let i = 1; i < rle.counts.length; i += 2) if (rle.counts[i] > 0) return false
  return true
}

/** Foreground pixel count, straight from the counts. */
export function rleArea(rle: MaskRLE): number {
  let area = 0
  for (let i = 1; i < rle.counts.length; i += 2) area += rle.counts[i]
  return area
}

/** Pixel-edge bounds `[x1, y1, x2, y2]` of the foreground, or null when empty.
 * A run spanning two or more columns covers the full column height between. */
export function rleBounds(rle: MaskRLE): BBox | null {
  const [height] = rle.size
  if (height <= 0) return null
  let xMin = Infinity
  let yMin = Infinity
  let xMax = -Infinity
  let yMax = -Infinity
  let index = 0
  for (let i = 0; i < rle.counts.length; i += 1) {
    const run = rle.counts[i]
    if (i % 2 === 1 && run > 0) {
      const first = index
      const last = index + run - 1
      const x0 = Math.floor(first / height)
      const x1 = Math.floor(last / height)
      xMin = Math.min(xMin, x0)
      xMax = Math.max(xMax, x1)
      if (x0 === x1) {
        yMin = Math.min(yMin, first % height)
        yMax = Math.max(yMax, last % height)
      } else {
        yMin = 0
        yMax = height - 1
      }
    }
    index += run
  }
  if (xMin === Infinity) return null
  return [xMin, yMin, xMax + 1, yMax + 1]
}

/** Sets (or, with `value` 0, clears) every pixel whose centre lies within
 * `radius` of `center`. */
export function paintDisc(bitmap: Bitmap, center: Point2D, radius: number, value: 0 | 1): void {
  const { width, height, bits } = bitmap
  const [cx, cy] = center
  const r = Math.max(radius, 0.5)
  const r2 = r * r
  const xStart = Math.max(0, Math.floor(cx - r))
  const xEnd = Math.min(width - 1, Math.ceil(cx + r))
  for (let x = xStart; x <= xEnd; x += 1) {
    const dx = x + 0.5 - cx
    const rest = r2 - dx * dx
    if (rest < 0) continue
    const half = Math.sqrt(rest)
    const y0 = Math.max(0, Math.ceil(cy - half - 0.5))
    const y1 = Math.min(height - 1, Math.floor(cy + half - 0.5))
    if (y1 >= y0) bits.fill(value, x * height + y0, x * height + y1 + 1)
  }
}

/** Rasterises a brush stroke: discs stamped along each segment at most half a
 * radius apart, so a fast drag leaves no gaps. */
export function paintStroke(
  bitmap: Bitmap,
  points: Point2D[],
  radius: number,
  value: 0 | 1,
): void {
  if (points.length === 0) return
  const step = Math.max(radius / 2, 0.5)
  paintDisc(bitmap, points[0], radius, value)
  for (let i = 1; i < points.length; i += 1) {
    const [ax, ay] = points[i - 1]
    const [bx, by] = points[i]
    const steps = Math.max(1, Math.ceil(Math.hypot(bx - ax, by - ay) / step))
    for (let s = 1; s <= steps; s += 1) {
      const t = s / steps
      paintDisc(bitmap, [ax + (bx - ax) * t, ay + (by - ay) * t], radius, value)
    }
  }
}

/** The foreground inside `bounds` as RGBA bytes (row-major, for ImageData),
 * `rgb` at `alpha` where set and transparent elsewhere. */
export function maskPixels(
  rle: MaskRLE,
  bounds: BBox,
  rgb: [number, number, number],
  alpha: number,
): Uint8ClampedArray<ArrayBuffer> {
  const [height] = rle.size
  const [bx0, by0, bx1, by1] = bounds
  const w = bx1 - bx0
  const h = by1 - by0
  const out = new Uint8ClampedArray(w * h * 4)
  let index = 0
  for (let i = 0; i < rle.counts.length; i += 1) {
    const run = rle.counts[i]
    if (i % 2 === 1) {
      for (let p = index; p < index + run; p += 1) {
        const x = Math.floor(p / height) - bx0
        const y = (p % height) - by0
        if (x < 0 || y < 0 || x >= w || y >= h) continue
        const o = (y * w + x) * 4
        out[o] = rgb[0]
        out[o + 1] = rgb[1]
        out[o + 2] = rgb[2]
        out[o + 3] = alpha
      }
    }
    index += run
  }
  return out
}

/** `#rrggbb` → `[r, g, b]`; anything else falls back to grey. */
export function hexToRgb(color: string): [number, number, number] {
  const match = /^#([0-9a-f]{6})$/i.exec(color)
  if (!match) return [128, 128, 128]
  const n = Number.parseInt(match[1], 16)
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}
