import { describe, expect, it } from 'vitest'

import type { BBoxShape, Shape } from '@/api/types'
import {
  boxAt,
  frameOf,
  groupTracks,
  markOutside,
  removeKeyframe,
  removeTrack,
  setKeyframe,
} from './tracks'

function kf(overrides: Partial<BBoxShape> = {}): BBoxShape {
  return {
    id: overrides.id ?? crypto.randomUUID(),
    type: 'bbox',
    class: 'car',
    attributes: {},
    confidence: null,
    frame: 0,
    track_id: 't1',
    keyframe: true,
    outside: false,
    bbox: [0, 0, 10, 10],
    ...overrides,
  }
}

describe('frameOf', () => {
  it('floors currentTime * fps', () => {
    expect(frameOf(1.999, 10)).toBe(19)
    expect(frameOf(0, 25)).toBe(0)
  })

  it('never goes negative', () => {
    expect(frameOf(-1, 25)).toBe(0)
  })
})

describe('groupTracks', () => {
  it('groups shapes sharing a track_id, sorted by frame', () => {
    const shapes: Shape[] = [
      kf({ id: 'b', frame: 10 }),
      kf({ id: 'a', frame: 0 }),
    ]
    const tracks = groupTracks(shapes)
    expect(tracks).toHaveLength(1)
    expect(tracks[0].trackId).toBe('t1')
    expect(tracks[0].single).toBe(false)
    expect(tracks[0].keyframes.map((s) => s.id)).toEqual(['a', 'b'])
  })

  it('treats a shape without track_id as its own single-frame track', () => {
    const shapes: Shape[] = [kf({ id: 's1', track_id: null, frame: 5 })]
    const tracks = groupTracks(shapes)
    expect(tracks).toEqual([
      { trackId: 's1', single: true, class: 'car', keyframes: [shapes[0]] },
    ])
  })

  it('ignores non-bbox shapes', () => {
    const span: Shape = {
      id: 's',
      type: 'span',
      class: 'x',
      attributes: {},
      confidence: null,
      start: 0,
      end: 1,
    }
    expect(groupTracks([span])).toEqual([])
  })
})

describe('boxAt', () => {
  it('is null before the first keyframe', () => {
    const track = groupTracks([kf({ frame: 10 })])[0]
    expect(boxAt(track, 5)).toBeNull()
  })

  it('is a solid keyframe box exactly on a keyframe frame', () => {
    const track = groupTracks([kf({ frame: 10, bbox: [1, 1, 2, 2] })])[0]
    expect(boxAt(track, 10)).toEqual({ bbox: [1, 1, 2, 2], keyframe: true })
  })

  it('interpolates linearly between two keyframes', () => {
    const shapes = [
      kf({ id: 'a', frame: 0, bbox: [0, 0, 10, 10] }),
      kf({ id: 'b', frame: 10, bbox: [10, 10, 20, 20] }),
    ]
    const track = groupTracks(shapes)[0]
    expect(boxAt(track, 5)).toEqual({ bbox: [5, 5, 15, 15], keyframe: false })
    expect(boxAt(track, 0)).toEqual({ bbox: [0, 0, 10, 10], keyframe: true })
    expect(boxAt(track, 10)).toEqual({ bbox: [10, 10, 20, 20], keyframe: true })
  })

  it('holds the last box after the last keyframe when not outside', () => {
    const track = groupTracks([kf({ frame: 0, bbox: [1, 1, 2, 2] })])[0]
    expect(boxAt(track, 50)).toEqual({ bbox: [1, 1, 2, 2], keyframe: false })
  })

  it('is null from an outside keyframe until the next keyframe', () => {
    const shapes = [
      kf({ id: 'a', frame: 0, bbox: [0, 0, 10, 10] }),
      kf({ id: 'b', frame: 10, outside: true, bbox: [0, 0, 10, 10] }),
      kf({ id: 'c', frame: 20, bbox: [20, 20, 30, 30] }),
    ]
    const track = groupTracks(shapes)[0]
    expect(boxAt(track, 5)?.keyframe).toBe(false)
    expect(boxAt(track, 10)).toBeNull()
    expect(boxAt(track, 15)).toBeNull()
    expect(boxAt(track, 20)).toEqual({ bbox: [20, 20, 30, 30], keyframe: true })
  })

  it('is null after the last keyframe when it is outside', () => {
    const track = groupTracks([kf({ frame: 0, outside: true })])[0]
    expect(boxAt(track, 100)).toBeNull()
  })

  it('single-frame boxes only appear on their own frame', () => {
    const track = groupTracks([kf({ track_id: null, frame: 3, bbox: [1, 2, 3, 4] })])[0]
    expect(boxAt(track, 3)).toEqual({ bbox: [1, 2, 3, 4], keyframe: true })
    expect(boxAt(track, 4)).toBeNull()
    expect(boxAt(track, 2)).toBeNull()
  })
})

describe('setKeyframe', () => {
  it('inserts a new keyframe for a new track', () => {
    const result = setKeyframe([], 't1', 0, [0, 0, 10, 10], 'car')
    expect(result).toHaveLength(1)
    const shape = result[0] as BBoxShape
    expect(shape.track_id).toBe('t1')
    expect(shape.frame).toBe(0)
    expect(shape.bbox).toEqual([0, 0, 10, 10])
    expect(shape.keyframe).toBe(true)
    expect(shape.outside).toBe(false)
    expect(shape.class).toBe('car')
  })

  it('replaces an existing keyframe at the same frame instead of duplicating', () => {
    const existing = kf({ id: 'a', frame: 5, bbox: [0, 0, 1, 1] })
    const result = setKeyframe([existing], 't1', 5, [9, 9, 19, 19], 'car')
    expect(result).toHaveLength(1)
    expect((result[0] as BBoxShape).id).toBe('a')
    expect((result[0] as BBoxShape).bbox).toEqual([9, 9, 19, 19])
  })

  it('clears outside when re-setting a keyframe', () => {
    const existing = kf({ id: 'a', frame: 5, outside: true })
    const result = setKeyframe([existing], 't1', 5, [1, 1, 2, 2], 'car')
    expect((result[0] as BBoxShape).outside).toBe(false)
  })
})

describe('removeKeyframe', () => {
  it('removes only the matching frame of that track', () => {
    const shapes = [kf({ id: 'a', frame: 0 }), kf({ id: 'b', frame: 10 })]
    const result = removeKeyframe(shapes, 't1', 0)
    expect(result.map((s) => (s as BBoxShape).id)).toEqual(['b'])
  })
})

describe('removeTrack', () => {
  it('removes every keyframe of the track and leaves others alone', () => {
    const shapes = [
      kf({ id: 'a', frame: 0, track_id: 't1' }),
      kf({ id: 'b', frame: 0, track_id: 't2' }),
    ]
    expect(removeTrack(shapes, 't1').map((s) => (s as BBoxShape).id)).toEqual(['b'])
  })
})

describe('markOutside', () => {
  it('sets outside on an existing keyframe at that frame', () => {
    const shapes = [kf({ id: 'a', frame: 5 })]
    const result = markOutside(shapes, 't1', 5)
    expect((result[0] as BBoxShape).outside).toBe(true)
  })

  it('inserts an outside keyframe holding the current box when none exists at that frame', () => {
    const shapes = [kf({ id: 'a', frame: 0, bbox: [1, 1, 2, 2] })]
    const result = markOutside(shapes, 't1', 8)
    expect(result).toHaveLength(2)
    const inserted = result.find((s) => (s as BBoxShape).frame === 8) as BBoxShape
    expect(inserted.outside).toBe(true)
    expect(inserted.bbox).toEqual([1, 1, 2, 2])
  })

  it('is a no-op for an unknown track', () => {
    expect(markOutside([], 'missing', 0)).toEqual([])
  })
})
