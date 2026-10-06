/**
 * Pure helpers for `segment` shapes on audio and time-series items (§5).
 *
 * Audio positions are integer milliseconds; time-series positions are values
 * on the CSV's time axis. The annotators only differ in what they draw under
 * the shared timeline, so everything here takes plain numbers.
 */

import type { Shape, SegmentShape } from '@/api/types'

export function isSegment(shape: Shape): shape is SegmentShape {
  return shape.type === 'segment'
}

/** The item's segments, earliest first. */
export function segmentsOf(shapes: readonly Shape[]): SegmentShape[] {
  return shapes.filter(isSegment).sort((a, b) => a.start - b.start || a.end - b.end)
}

/** Replace the segment with the same id, or append it. */
export function upsertSegment(shapes: readonly Shape[], segment: SegmentShape): Shape[] {
  const index = shapes.findIndex((shape) => shape.id === segment.id)
  if (index === -1) return [...shapes, segment]
  return shapes.map((shape, i) => (i === index ? segment : shape))
}

export function removeShapeById(shapes: readonly Shape[], id: string): Shape[] {
  return shapes.filter((shape) => shape.id !== id)
}

/**
 * Order and clamp an interval to `[min, max]`. Returns null when what is left
 * is empty; for audio (`whole`) both ends are rounded to milliseconds.
 */
export function normaliseInterval(
  a: number,
  b: number,
  min: number,
  max: number,
  whole: boolean,
): { start: number; end: number } | null {
  let start = Math.max(min, Math.min(a, b))
  let end = Math.min(max, Math.max(a, b))
  if (whole) {
    start = Math.round(start)
    end = Math.round(end)
  }
  return end > start ? { start, end } : null
}

/** Position on the axis for a pointer at `x` pixels across a `width`-pixel track. */
export function xToPosition(x: number, width: number, min: number, max: number): number {
  if (width <= 0) return min
  const fraction = Math.min(1, Math.max(0, x / width))
  return min + fraction * (max - min)
}

/** Percentage across the track for a position on the axis (for CSS `left` / `width`). */
export function positionToPercent(position: number, min: number, max: number): number {
  if (max <= min) return 0
  return ((position - min) / (max - min)) * 100
}

/** `m:ss.mmm` for milliseconds, `h:mm:ss.mmm` past an hour. */
export function formatClock(ms: number): string {
  const total = Math.max(0, Math.round(ms))
  const millis = total % 1000
  const seconds = Math.floor(total / 1000) % 60
  const minutes = Math.floor(total / 60_000) % 60
  const hours = Math.floor(total / 3_600_000)
  const tail = `${String(seconds).padStart(2, '0')}.${String(millis).padStart(3, '0')}`
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, '0')}:${tail}`
    : `${minutes}:${tail}`
}

/** Min/max pairs over `buckets` equal slices of `samples`, for drawing a waveform. */
export function computePeaks(samples: ArrayLike<number>, buckets: number): Array<[number, number]> {
  const count = Math.max(1, Math.floor(buckets))
  const peaks: Array<[number, number]> = []
  const size = samples.length / count
  for (let bucket = 0; bucket < count; bucket += 1) {
    const from = Math.floor(bucket * size)
    const to = Math.max(from + 1, Math.floor((bucket + 1) * size))
    let low = 0
    let high = 0
    for (let i = from; i < to && i < samples.length; i += 1) {
      const value = samples[i] ?? 0
      if (value < low) low = value
      if (value > high) high = value
    }
    peaks.push([low, high])
  }
  return peaks
}
