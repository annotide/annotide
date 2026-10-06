/**
 * Superpixels for the mask tools (TOOL): SLIC (Achanta et al., 2012) over
 * CIELAB, computed in the browser on a downscaled copy of the image.
 *
 * Client-side on purpose: the pixels are already in the browser through the
 * signed URL, so no backend job has to fetch the customer's media, and no
 * image-processing runtime dependency (scikit-image) joins the backend image.
 *
 * The segmentation lives at a working resolution of at most
 * `MAX_WORKING_PIXELS`; a click maps into it, and filling a superpixel into a
 * full-resolution mask (`fillSuperpixels`) samples it per image pixel.
 */

import type { Point2D } from '@/api/types'
import type { Bitmap } from './mask'

/** Pixels SLIC runs on; larger images are downscaled first. Keeps one pass
 * well under a second on a laptop. */
export const MAX_WORKING_PIXELS = 1_000_000
export const DEFAULT_SEGMENTS = 1200
export const MIN_SEGMENTS = 100
export const MAX_SEGMENTS = 5000
/** Colour vs. space weight: higher gives rounder, more regular superpixels. */
const COMPACTNESS = 10
const ITERATIONS = 10

export interface Segmentation {
  /** Working resolution. */
  width: number
  height: number
  /** Working pixels per image pixel (<= 1). */
  scale: number
  /** Superpixel id per working pixel, row-major, 0..count-1. */
  labels: Int32Array
  count: number
  /** Per superpixel `[x0, y0, x1, y1]` in working pixels, end exclusive. */
  bounds: Int32Array
}

/** Working size for an image: its own when small enough, else downscaled
 * keeping the aspect ratio. */
export function workingSize(
  imageWidth: number,
  imageHeight: number,
  maxPixels = MAX_WORKING_PIXELS,
): { width: number; height: number; scale: number } {
  const scale = Math.min(1, Math.sqrt(maxPixels / (imageWidth * imageHeight)))
  return {
    width: Math.max(1, Math.round(imageWidth * scale)),
    height: Math.max(1, Math.round(imageHeight * scale)),
    scale,
  }
}

const SRGB_TO_LINEAR = new Float32Array(256)
for (let i = 0; i < 256; i += 1) {
  const c = i / 255
  SRGB_TO_LINEAR[i] = c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
}

function labF(t: number): number {
  return t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116
}

/** RGBA bytes → three planar CIELAB channels (D65). */
function toLab(rgba: Uint8ClampedArray, n: number): [Float32Array, Float32Array, Float32Array] {
  const L = new Float32Array(n)
  const A = new Float32Array(n)
  const B = new Float32Array(n)
  for (let i = 0; i < n; i += 1) {
    const r = SRGB_TO_LINEAR[rgba[i * 4]]
    const g = SRGB_TO_LINEAR[rgba[i * 4 + 1]]
    const b = SRGB_TO_LINEAR[rgba[i * 4 + 2]]
    const fx = labF((0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047)
    const fy = labF(0.2126 * r + 0.7152 * g + 0.0722 * b)
    const fz = labF((0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883)
    L[i] = 116 * fy - 16
    A[i] = 500 * (fx - fy)
    B[i] = 200 * (fy - fz)
  }
  return [L, A, B]
}

/** SLIC superpixels of a `width × height` RGBA image (row-major), about
 * `segments` of them, each one 4-connected. */
export function slic(
  rgba: Uint8ClampedArray,
  width: number,
  height: number,
  segments: number,
): { labels: Int32Array; count: number } {
  const n = width * height
  const [L, A, B] = toLab(rgba, n)
  const step = Math.max(1, Math.sqrt(n / Math.max(1, segments)))

  // Seeds on a regular grid, each moved to the lowest gradient in its 3×3.
  const seeds: number[] = []
  for (let y = step / 2; y < height; y += step) {
    for (let x = step / 2; x < width; x += step) {
      let bx = Math.floor(x)
      let by = Math.floor(y)
      let best = Infinity
      for (let dy = -1; dy <= 1; dy += 1) {
        for (let dx = -1; dx <= 1; dx += 1) {
          const px = Math.floor(x) + dx
          const py = Math.floor(y) + dy
          if (px < 1 || py < 1 || px >= width - 1 || py >= height - 1) continue
          const i = py * width + px
          const gx = L[i + 1] - L[i - 1]
          const gy = L[i + width] - L[i - width]
          const gradient = gx * gx + gy * gy
          if (gradient < best) {
            best = gradient
            bx = px
            by = py
          }
        }
      }
      const i = by * width + bx
      seeds.push(L[i], A[i], B[i], bx, by)
    }
  }
  const k = seeds.length / 5
  const centers = Float64Array.from(seeds)
  const labels = new Int32Array(n).fill(-1)
  const distances = new Float32Array(n)
  const spatial = (COMPACTNESS / step) ** 2
  const reach = Math.ceil(step)
  const sums = new Float64Array(k * 6)

  for (let iteration = 0; iteration < ITERATIONS; iteration += 1) {
    distances.fill(Infinity)
    for (let c = 0; c < k; c += 1) {
      const cl = centers[c * 5]
      const ca = centers[c * 5 + 1]
      const cb = centers[c * 5 + 2]
      const cx = centers[c * 5 + 3]
      const cy = centers[c * 5 + 4]
      const x0 = Math.max(0, Math.floor(cx - reach))
      const x1 = Math.min(width, Math.ceil(cx + reach))
      const y0 = Math.max(0, Math.floor(cy - reach))
      const y1 = Math.min(height, Math.ceil(cy + reach))
      for (let y = y0; y < y1; y += 1) {
        const dy = y - cy
        let i = y * width + x0
        for (let x = x0; x < x1; x += 1, i += 1) {
          const dl = L[i] - cl
          const da = A[i] - ca
          const db = B[i] - cb
          const dx = x - cx
          const d = dl * dl + da * da + db * db + (dx * dx + dy * dy) * spatial
          if (d < distances[i]) {
            distances[i] = d
            labels[i] = c
          }
        }
      }
    }
    sums.fill(0)
    for (let y = 0, i = 0; y < height; y += 1) {
      for (let x = 0; x < width; x += 1, i += 1) {
        const c = labels[i]
        if (c < 0) continue
        const o = c * 6
        sums[o] += L[i]
        sums[o + 1] += A[i]
        sums[o + 2] += B[i]
        sums[o + 3] += x
        sums[o + 4] += y
        sums[o + 5] += 1
      }
    }
    for (let c = 0; c < k; c += 1) {
      const o = c * 6
      const count = sums[o + 5]
      if (count === 0) continue
      for (let j = 0; j < 5; j += 1) centers[c * 5 + j] = sums[o + j] / count
    }
  }

  return enforceConnectivity(labels, width, height, Math.max(1, Math.floor(n / k / 4)))
}

/** Relabels 4-connected components 0..count-1; a component smaller than
 * `minSize` (or a pixel no centre reached) joins the component before it. */
function enforceConnectivity(
  labels: Int32Array,
  width: number,
  height: number,
  minSize: number,
): { labels: Int32Array; count: number } {
  const n = width * height
  const out = new Int32Array(n).fill(-1)
  const queue = new Int32Array(n)
  let count = 0
  for (let start = 0; start < n; start += 1) {
    if (out[start] >= 0) continue
    const original = labels[start]
    // A labelled neighbour to join if this component turns out too small.
    const sx = start % width
    let adjacent = -1
    if (sx > 0 && out[start - 1] >= 0) adjacent = out[start - 1]
    else if (start >= width && out[start - width] >= 0) adjacent = out[start - width]

    let head = 0
    let tail = 0
    queue[tail++] = start
    out[start] = count
    const visit = (j: number) => {
      if (out[j] >= 0 || labels[j] !== original) return
      out[j] = count
      queue[tail++] = j
    }
    while (head < tail) {
      const i = queue[head++]
      const x = i % width
      if (x > 0) visit(i - 1)
      if (x < width - 1) visit(i + 1)
      if (i >= width) visit(i - width)
      if (i + width < n) visit(i + width)
    }
    if ((tail < minSize || original < 0) && adjacent >= 0) {
      for (let q = 0; q < tail; q += 1) out[queue[q]] = adjacent
    } else {
      count += 1
    }
  }
  return { labels: out, count }
}

/** Superpixels of an image already scaled to its working size (see
 * `workingSize`). */
export function segment(
  rgba: Uint8ClampedArray,
  width: number,
  height: number,
  scale: number,
  segments = DEFAULT_SEGMENTS,
): Segmentation {
  const { labels, count } = slic(rgba, width, height, segments)
  const bounds = new Int32Array(count * 4)
  for (let c = 0; c < count; c += 1) {
    bounds[c * 4] = width
    bounds[c * 4 + 1] = height
  }
  for (let y = 0, i = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1, i += 1) {
      const o = labels[i] * 4
      if (x < bounds[o]) bounds[o] = x
      if (y < bounds[o + 1]) bounds[o + 1] = y
      if (x + 1 > bounds[o + 2]) bounds[o + 2] = x + 1
      if (y + 1 > bounds[o + 3]) bounds[o + 3] = y + 1
    }
  }
  return { width, height, scale, labels, count, bounds }
}

/** The superpixel under an image-pixel point; -1 outside the image. */
export function labelAt(seg: Segmentation, [x, y]: Point2D): number {
  const wx = Math.floor(x * seg.scale)
  const wy = Math.floor(y * seg.scale)
  if (wx < 0 || wy < 0 || wx >= seg.width || wy >= seg.height) return -1
  return seg.labels[wy * seg.width + wx]
}

/** The superpixels a straight pointer path from `from` to `to` crosses
 * (image pixels), sampled once per working pixel so a fast drag, whose move
 * events land far apart, skips none. */
export function labelsAlong(seg: Segmentation, from: Point2D, to: Point2D): number[] {
  const steps = Math.max(
    1,
    Math.ceil(Math.hypot(to[0] - from[0], to[1] - from[1]) * seg.scale),
  )
  const found = new Set<number>()
  for (let i = 0; i <= steps; i += 1) {
    const t = i / steps
    const label = labelAt(seg, [from[0] + (to[0] - from[0]) * t, from[1] + (to[1] - from[1]) * t])
    if (label >= 0) found.add(label)
  }
  return [...found]
}

/** Sets (or clears) every image pixel of `labels` in a full-resolution
 * column-major mask bitmap, visiting only each superpixel's bounds. */
export function fillSuperpixels(
  bitmap: Bitmap,
  seg: Segmentation,
  labels: Iterable<number>,
  value: 0 | 1,
): void {
  const { width: iw, height: ih, bits } = bitmap
  const toWorking = (v: number, limit: number) => Math.min(limit - 1, Math.floor(v * seg.scale))
  for (const label of labels) {
    if (label < 0 || label >= seg.count) continue
    const o = label * 4
    const x0 = Math.max(0, Math.floor(seg.bounds[o] / seg.scale))
    const y0 = Math.max(0, Math.floor(seg.bounds[o + 1] / seg.scale))
    const x1 = Math.min(iw, Math.ceil(seg.bounds[o + 2] / seg.scale))
    const y1 = Math.min(ih, Math.ceil(seg.bounds[o + 3] / seg.scale))
    for (let x = x0; x < x1; x += 1) {
      const wx = toWorking(x, seg.width)
      for (let y = y0; y < y1; y += 1) {
        if (seg.labels[toWorking(y, seg.height) * seg.width + wx] === label) bits[x * ih + y] = value
      }
    }
  }
}

/** RGBA (row-major, working size) with `rgba` on every pixel whose right or
 * lower neighbour lies in another superpixel, transparent elsewhere. */
export function boundaryPixels(
  seg: Segmentation,
  rgba: [number, number, number, number],
): Uint8ClampedArray<ArrayBuffer> {
  const { width, height, labels } = seg
  const out = new Uint8ClampedArray(width * height * 4)
  for (let y = 0, i = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1, i += 1) {
      const edge =
        (x < width - 1 && labels[i] !== labels[i + 1]) ||
        (y < height - 1 && labels[i] !== labels[i + width])
      if (!edge) continue
      out.set(rgba, i * 4)
    }
  }
  return out
}

/** RGBA for the pixels of `label` inside its bounds (row-major), and those
 * bounds; for the gesture preview. */
export function superpixelPixels(
  seg: Segmentation,
  label: number,
  rgba: [number, number, number, number],
): { x: number; y: number; width: number; height: number; data: Uint8ClampedArray<ArrayBuffer> } {
  const o = label * 4
  const [x0, y0, x1, y1] = [seg.bounds[o], seg.bounds[o + 1], seg.bounds[o + 2], seg.bounds[o + 3]]
  const w = x1 - x0
  const h = y1 - y0
  const data = new Uint8ClampedArray(w * h * 4)
  for (let y = 0; y < h; y += 1) {
    for (let x = 0; x < w; x += 1) {
      if (seg.labels[(y0 + y) * seg.width + x0 + x] === label) data.set(rgba, (y * w + x) * 4)
    }
  }
  return { x: x0, y: y0, width: w, height: h, data }
}
