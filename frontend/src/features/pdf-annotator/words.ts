/**
 * Word boxes from a pdf.js text layer, in the page's own coordinate space
 * (PDF points at scale 1, top-left origin, y down — the space shapes are
 * stored in, CONTRACTS.md "PDF items"). Pure, so it is tested without pdf.js.
 */
import type { BBox } from '@/api/types'

/** The fields of pdf.js's `TextItem` this module reads. */
export interface TextRun {
  str: string
  /** Text-space → page-space matrix `[a, b, c, d, e, f]`. */
  transform: number[]
  /** Advance width of the run in page units. */
  width: number
}

export interface Word {
  text: string
  bbox: BBox
}

type Matrix = [number, number, number, number, number, number]

/** `Util.transform` from pdf.js: the product m1 × m2 of two affine matrices. */
export function multiply(m1: number[], m2: number[]): Matrix {
  return [
    m1[0] * m2[0] + m1[2] * m2[1],
    m1[1] * m2[0] + m1[3] * m2[1],
    m1[0] * m2[2] + m1[2] * m2[3],
    m1[1] * m2[2] + m1[3] * m2[3],
    m1[0] * m2[4] + m1[2] * m2[5] + m1[4],
    m1[1] * m2[4] + m1[3] * m2[5] + m1[5],
  ]
}

/**
 * Splits each run into words and places them by character share of the
 * run's advance width — exact for monospace, close enough for picking the
 * words under a box. `viewportTransform` is the scale-1 viewport's matrix,
 * which flips y and applies the page rotation. Rotated runs get the
 * axis-aligned envelope of their rotated baseline box.
 */
export function wordBoxes(runs: TextRun[], viewportTransform: number[]): Word[] {
  const words: Word[] = []
  for (const run of runs) {
    const chars = Array.from(run.str)
    if (chars.length === 0 || run.width <= 0) continue
    const m = multiply(viewportTransform, run.transform)
    const fontHeight = Math.hypot(m[2], m[3])
    // Unit vectors along the baseline and "up" in viewport space.
    const along = Math.hypot(m[0], m[1]) || 1
    const ux = m[0] / along
    const uy = m[1] / along
    const scale = Math.hypot(viewportTransform[0], viewportTransform[1]) || 1
    const advance = run.width * scale
    const pattern = /\S+/gu
    const text = chars.join('')
    let match: RegExpExecArray | null
    while ((match = pattern.exec(text)) !== null) {
      const start = Array.from(text.slice(0, match.index)).length
      const length = Array.from(match[0]).length
      const from = (advance * start) / chars.length
      const to = (advance * (start + length)) / chars.length
      // Corners: baseline from→to, and the same raised by the font height.
      const corners: Array<[number, number]> = [from, to].flatMap((d) => {
        const x = m[4] + ux * d
        const y = m[5] + uy * d
        return [
          [x, y],
          [x + uy * fontHeight, y - ux * fontHeight],
        ] as Array<[number, number]>
      })
      const xs = corners.map((c) => c[0])
      const ys = corners.map((c) => c[1])
      words.push({
        text: match[0],
        bbox: [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)],
      })
    }
  }
  return words
}

/**
 * The words whose centres fall inside `box`, in reading order (lines top to
 * bottom, words left to right), joined by single spaces; `undefined` when
 * none do — a scanned page without a text layer, or an empty area.
 */
export function textInBox(words: Word[], box: BBox): string | undefined {
  const inside = words.filter((word) => {
    const cx = (word.bbox[0] + word.bbox[2]) / 2
    const cy = (word.bbox[1] + word.bbox[3]) / 2
    return cx >= box[0] && cx <= box[2] && cy >= box[1] && cy <= box[3]
  })
  if (inside.length === 0) return undefined
  const height = (word: Word): number => word.bbox[3] - word.bbox[1]
  const sorted = [...inside].sort((a, b) => a.bbox[1] - b.bbox[1])
  const lines: Word[][] = []
  for (const word of sorted) {
    const line = lines[lines.length - 1]
    const centre = (word.bbox[1] + word.bbox[3]) / 2
    if (line && Math.abs(centre - (line[0].bbox[1] + line[0].bbox[3]) / 2) < height(line[0]) / 2) {
      line.push(word)
    } else {
      lines.push([word])
    }
  }
  return lines
    .map((line) =>
      line
        .sort((a, b) => a.bbox[0] - b.bbox[0])
        .map((word) => word.text)
        .join(' '),
    )
    .join(' ')
}

/** Most boxes a span may carry (CONTRACTS.md "Entity spans on PDFs"). */
export const MAX_SPAN_BOXES = 256

const gap = (a: number, b0: number, b1: number): number => (a < b0 ? b0 - a : a > b1 ? a - b1 : 0)

/**
 * Index of the word under `point`; with `nearest`, the closest word when the
 * point is in a gap (so a drag may end between words). `-1` when there is none.
 */
export function wordAt(words: Word[], point: [number, number], nearest = false): number {
  let best = -1
  let bestDistance = Infinity
  words.forEach((word, index) => {
    const distance = Math.hypot(
      gap(point[0], word.bbox[0], word.bbox[2]),
      gap(point[1], word.bbox[1], word.bbox[3]),
    )
    if (distance < bestDistance) {
      best = index
      bestDistance = distance
    }
  })
  return nearest || bestDistance === 0 ? best : -1
}

/** The words from index `a` to index `b` inclusive, in either order. */
export function wordRun(words: Word[], a: number, b: number): Word[] {
  return words.slice(Math.max(0, Math.min(a, b)), Math.max(a, b) + 1)
}

/**
 * One box per line of `run` (words in reading order): consecutive words whose
 * vertical centres are within half the taller word's height of each other
 * share a line, and the box is the union of their boxes.
 */
export function lineBoxes(run: Word[]): BBox[] {
  const centre = (word: Word): number => (word.bbox[1] + word.bbox[3]) / 2
  const height = (word: Word): number => word.bbox[3] - word.bbox[1]
  const boxes: BBox[] = []
  let previous: Word | null = null
  for (const word of run) {
    const current = boxes[boxes.length - 1]
    if (
      previous &&
      current &&
      Math.abs(centre(word) - centre(previous)) <= Math.max(height(word), height(previous)) / 2
    ) {
      current[0] = Math.min(current[0], word.bbox[0])
      current[1] = Math.min(current[1], word.bbox[1])
      current[2] = Math.max(current[2], word.bbox[2])
      current[3] = Math.max(current[3], word.bbox[3])
    } else {
      boxes.push([...word.bbox])
    }
    previous = word
  }
  return boxes
}

/** The `boxes` and `text` of a span over the words `a`..`b`; null when it cannot be stored. */
export function spanGeometry(
  words: Word[],
  a: number,
  b: number,
): { boxes: BBox[]; text: string } | null {
  const run = wordRun(words, a, b)
  // A word that pokes past the page edge would give a negative coordinate,
  // which the API refuses; clamp to the page's top-left as it does for boxes.
  const boxes = lineBoxes(run).map(
    ([x0, y0, x1, y1]): BBox => [Math.max(0, x0), Math.max(0, y0), Math.max(0, x1), Math.max(0, y1)],
  )
  if (boxes.length === 0 || boxes.length > MAX_SPAN_BOXES) return null
  if (boxes.some((box) => box[2] <= box[0] || box[3] <= box[1])) return null
  return { boxes, text: run.map((word) => word.text).join(' ') }
}
