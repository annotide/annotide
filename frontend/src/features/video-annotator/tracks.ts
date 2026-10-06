/**
 * Pure helpers for video bounding-box tracks (docs/CONTRACTS.md, "Video
 * items"). A track is the set of `bbox` shapes sharing a `track_id`; a shape
 * with no `track_id` is a single-frame box, visible only on its own frame.
 * Only keyframes are stored — frames between them are interpolated linearly
 * per bbox coordinate, never persisted.
 */
import type { BBox, BBoxShape, Shape } from '@/api/types'

export interface Track {
  /** `track_id` for a real track, or the shape's own `id` for a single-frame box. */
  trackId: string
  /** True when the underlying shape has no `track_id` (one frame only, no hold/interpolation). */
  single: boolean
  class: string
  /** Keyframe shapes, ascending by `frame`. */
  keyframes: BBoxShape[]
}

export interface FrameBox {
  bbox: BBox
  /** False when this is an interpolated or held-over box, not a stored keyframe. */
  keyframe: boolean
}

function isBBoxShape(shape: Shape): shape is BBoxShape {
  return shape.type === 'bbox'
}

function frameNumber(shape: BBoxShape): number {
  return shape.frame ?? 0
}

/** Groups a result's bbox shapes into tracks, ordered by first appearance. */
export function groupTracks(shapes: Shape[]): Track[] {
  const order: string[] = []
  const byKey = new Map<string, BBoxShape[]>()
  for (const shape of shapes) {
    if (!isBBoxShape(shape)) continue
    const key = shape.track_id ?? shape.id
    let list = byKey.get(key)
    if (!list) {
      list = []
      byKey.set(key, list)
      order.push(key)
    }
    list.push(shape)
  }
  return order.map((key) => {
    const keyframes = [...(byKey.get(key) ?? [])].sort((a, b) => frameNumber(a) - frameNumber(b))
    return {
      trackId: key,
      single: !keyframes[0]?.track_id,
      class: keyframes[0]?.class ?? '',
      keyframes,
    }
  })
}

function lerp(a: number, b: number, t: number): number {
  return a + (b - a) * t
}

function interpolateBBox(a: BBox, b: BBox, t: number): BBox {
  return [lerp(a[0], b[0], t), lerp(a[1], b[1], t), lerp(a[2], b[2], t), lerp(a[3], b[3], t)]
}

/**
 * The box shown for `track` at `frame`, or null when nothing is visible:
 * before the track's first keyframe, from an `outside` keyframe until the
 * next one (inclusive of the outside frame itself), and — for single-frame
 * boxes only — on any frame other than their own.
 */
export function boxAt(track: Track, frame: number): FrameBox | null {
  const kfs = track.keyframes
  if (kfs.length === 0) return null

  if (track.single) {
    const kf = kfs[0]
    return frameNumber(kf) === frame ? { bbox: kf.bbox, keyframe: true } : null
  }

  if (frame < frameNumber(kfs[0])) return null

  let idx = -1
  for (let i = 0; i < kfs.length; i += 1) {
    if (frameNumber(kfs[i]) <= frame) idx = i
    else break
  }
  const current = kfs[idx]
  if (current.outside) return null
  if (frameNumber(current) === frame) return { bbox: current.bbox, keyframe: true }

  const next = kfs[idx + 1]
  if (!next) return { bbox: current.bbox, keyframe: false }

  const t = (frame - frameNumber(current)) / (frameNumber(next) - frameNumber(current))
  return { bbox: interpolateBBox(current.bbox, next.bbox, t), keyframe: false }
}

/** The frame on screen for a given playback time, per CONTRACTS.md. */
export function frameOf(time: number, fps: number): number {
  return Math.max(0, Math.floor(time * fps))
}

/** Inserts a keyframe at `frame` for `trackId`, or replaces one already there. */
export function setKeyframe(
  shapes: Shape[],
  trackId: string,
  frame: number,
  bbox: BBox,
  className: string,
): Shape[] {
  const existingIdx = shapes.findIndex(
    (shape) => isBBoxShape(shape) && (shape.track_id ?? shape.id) === trackId && shape.frame === frame,
  )
  if (existingIdx !== -1) {
    const existing = shapes[existingIdx] as BBoxShape
    const updated: BBoxShape = { ...existing, bbox, keyframe: true, outside: false }
    return shapes.map((shape, i) => (i === existingIdx ? updated : shape))
  }
  const created: BBoxShape = {
    id: crypto.randomUUID(),
    type: 'bbox',
    class: className,
    attributes: {},
    confidence: null,
    frame,
    track_id: trackId,
    keyframe: true,
    outside: false,
    bbox,
  }
  return [...shapes, created]
}

/** Removes the keyframe of `trackId` at `frame`, if any. */
export function removeKeyframe(shapes: Shape[], trackId: string, frame: number): Shape[] {
  return shapes.filter(
    (shape) => !(isBBoxShape(shape) && (shape.track_id ?? shape.id) === trackId && shape.frame === frame),
  )
}

/**
 * Marks the track as leaving the view from `frame` on: sets `outside` on an
 * existing keyframe there, or inserts one holding the box the track already
 * shows at that frame.
 */
export function markOutside(shapes: Shape[], trackId: string, frame: number): Shape[] {
  const track = groupTracks(shapes).find((t) => t.trackId === trackId)
  if (!track) return shapes

  const existingIdx = shapes.findIndex(
    (shape) => isBBoxShape(shape) && (shape.track_id ?? shape.id) === trackId && shape.frame === frame,
  )
  if (existingIdx !== -1) {
    const existing = shapes[existingIdx] as BBoxShape
    const updated: BBoxShape = { ...existing, outside: true, keyframe: true }
    return shapes.map((shape, i) => (i === existingIdx ? updated : shape))
  }

  const at = boxAt(track, frame)
  const bbox = at?.bbox ?? track.keyframes[track.keyframes.length - 1]?.bbox
  if (!bbox) return shapes

  const created: BBoxShape = {
    id: crypto.randomUUID(),
    type: 'bbox',
    class: track.class,
    attributes: {},
    confidence: null,
    frame,
    track_id: trackId,
    keyframe: true,
    outside: true,
    bbox,
  }
  return [...shapes, created]
}

/** Removes every keyframe of `trackId` (the whole track). */
export function removeTrack(shapes: Shape[], trackId: string): Shape[] {
  return shapes.filter((shape) => !(isBBoxShape(shape) && (shape.track_id ?? shape.id) === trackId))
}
