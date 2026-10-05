/**
 * Text offsets for span annotation (TOOL).
 *
 * Spans are stored in Unicode code points (CONTRACTS.md, text items) — the
 * indices of `Array.from(text)` and of Python's `str` — while the DOM and
 * JavaScript strings count UTF-16 units. An emoji or any character outside the
 * Basic Multilingual Plane is one code point but two units, so every offset
 * that crosses between the page and a stored span goes through here.
 */

import type { RelationShape, Shape, TextSpanShape } from '@/api/types'

/** Code-point index of the UTF-16 offset `utf16` in `text`. */
export function utf16ToCodePoint(text: string, utf16: number): number {
  return Array.from(text.slice(0, utf16)).length
}

/** UTF-16 offset of the code-point index `codePoint` in `text`. */
export function codePointToUtf16(text: string, codePoint: number): number {
  return Array.from(text).slice(0, codePoint).join('').length
}

/** `[start, end)` in code points, shrunk to exclude surrounding whitespace;
 * null when nothing but whitespace (or nothing) is selected. */
export function trimRange(
  chars: readonly string[],
  start: number,
  end: number,
): [number, number] | null {
  let from = Math.max(0, Math.min(start, end))
  let to = Math.min(chars.length, Math.max(start, end))
  while (from < to && /\s/u.test(chars[from])) from += 1
  while (to > from && /\s/u.test(chars[to - 1])) to -= 1
  return from < to ? [from, to] : null
}

export function isSpan(shape: Shape): shape is TextSpanShape {
  return shape.type === 'span' && shape.boxes == null
}

export function isRelation(shape: Shape): shape is RelationShape {
  return shape.type === 'relation'
}

export interface Segment {
  /** Code-point range of this run of text. */
  start: number
  end: number
  /** Spans covering the whole run, outermost (longest) first. */
  spans: TextSpanShape[]
}

/**
 * Cut the text at every span boundary, so each run is covered by a fixed set
 * of spans. Overlapping and nested spans (TOOL) fall out naturally: a run
 * inside two spans lists both.
 */
export function segmentText(length: number, spans: readonly TextSpanShape[]): Segment[] {
  const cuts = new Set<number>([0, length])
  for (const span of spans) {
    if (span.start < length) cuts.add(Math.max(0, span.start))
    if (span.end > 0) cuts.add(Math.min(length, span.end))
  }
  const points = [...cuts].sort((a, b) => a - b)
  const segments: Segment[] = []
  for (let i = 0; i < points.length - 1; i += 1) {
    const start = points[i]
    const end = points[i + 1]
    const covering = spans
      .filter((span) => span.start <= start && span.end >= end)
      .sort((a, b) => b.end - b.start - (a.end - a.start))
    segments.push({ start, end, spans: covering })
  }
  return segments
}

/** Remove a shape; removing a span also removes every relation touching it. */
export function removeShape(shapes: readonly Shape[], id: string): Shape[] {
  return shapes.filter(
    (shape) => shape.id !== id && !(isRelation(shape) && (shape.from === id || shape.to === id)),
  )
}
