import { describe, expect, it } from 'vitest'

import type { SegmentShape, Shape } from '@/api/types'
import {
  computePeaks,
  formatClock,
  normaliseInterval,
  positionToPercent,
  removeShapeById,
  segmentsOf,
  upsertSegment,
  xToPosition,
} from './segments'
import { downsample, parseSeries, splitCsvLine } from './series'

function seg(id: string, start: number, end: number): SegmentShape {
  return { id, type: 'segment', class: 'speech', attributes: {}, confidence: null, start, end }
}

describe('segments', () => {
  it('sorts, upserts and removes', () => {
    const shapes: Shape[] = [seg('b', 500, 900), seg('a', 0, 400)]
    expect(segmentsOf(shapes).map((s) => s.id)).toEqual(['a', 'b'])
    const moved = upsertSegment(shapes, seg('b', 100, 200))
    expect(moved).toHaveLength(2)
    expect(segmentsOf(moved).map((s) => s.id)).toEqual(['a', 'b'])
    expect(upsertSegment(shapes, seg('c', 1, 2))).toHaveLength(3)
    expect(removeShapeById(shapes, 'a').map((s) => s.id)).toEqual(['b'])
  })

  it('orders, clamps and rounds intervals', () => {
    expect(normaliseInterval(900.6, 100.2, 0, 800, true)).toEqual({ start: 100, end: 800 })
    expect(normaliseInterval(1.25, 0.5, 0, 10, false)).toEqual({ start: 0.5, end: 1.25 })
    expect(normaliseInterval(5, 5.2, 0, 10, true)).toBeNull()
    expect(normaliseInterval(-5, -1, 0, 10, false)).toBeNull()
  })

  it('maps between pixels, positions and percentages', () => {
    expect(xToPosition(50, 200, 0, 1000)).toBe(250)
    expect(xToPosition(-10, 200, 0, 1000)).toBe(0)
    expect(xToPosition(500, 200, 0, 1000)).toBe(1000)
    expect(xToPosition(10, 0, 3, 9)).toBe(3)
    expect(positionToPercent(250, 0, 1000)).toBe(25)
    expect(positionToPercent(5, 5, 5)).toBe(0)
  })

  it('formats clocks', () => {
    expect(formatClock(0)).toBe('0:00.000')
    expect(formatClock(61_005)).toBe('1:01.005')
    expect(formatClock(3_723_004)).toBe('1:02:03.004')
  })

  it('computes min/max peaks per bucket', () => {
    expect(computePeaks([0.1, -0.5, 0.9, -0.2], 2)).toEqual([
      [-0.5, 0.1],
      [-0.2, 0.9],
    ])
    expect(computePeaks([], 3)).toEqual([
      [0, 0],
      [0, 0],
      [0, 0],
    ])
  })
})

describe('series', () => {
  it('splits quoted CSV cells', () => {
    expect(splitCsvLine('a,"b, c","d ""e"""')).toEqual(['a', 'b, c', 'd "e"'])
  })

  it('parses numbers, gaps and timestamps', () => {
    const numeric = parseSeries('t,x,y\n0,1,\n0.5,2,3\n1,,4\n')
    expect(numeric).toEqual({
      ok: true,
      series: {
        timeLabel: 't',
        timestamps: false,
        time: [0, 0.5, 1],
        channels: [
          { name: 'x', values: [1, 2, null] },
          { name: 'y', values: [null, 3, 4] },
        ],
      },
    })
    const dated = parseSeries('ts,v\n2026-10-01T00:00:00Z,1\n2026-10-01T00:00:01Z,2')
    expect(dated.ok && dated.series.timestamps).toBe(true)
    expect(dated.ok && dated.series.time[1]! - dated.series.time[0]!).toBe(1000)
  })

  it('explains what is wrong', () => {
    expect(parseSeries('t,x')).toEqual({ ok: false, error: 'needs a header row and at least one row' })
    expect(parseSeries('t\n1')).toMatchObject({ ok: false })
    expect(parseSeries('t,x\nsoon,1')).toMatchObject({ ok: false, error: expect.stringContaining('row 2') })
    expect(parseSeries('t,x\n2,1\n1,1')).toMatchObject({ ok: false, error: 'row 3: time goes backwards' })
    expect(parseSeries('t,x\n1,1\n2026-01-01,1')).toMatchObject({
      ok: false,
      error: expect.stringContaining('mixes'),
    })
  })

  it('downsamples keeping spikes and dropping gaps', () => {
    const time = Array.from({ length: 100 }, (_, i) => i)
    const values = time.map((i) => (i === 37 ? 50 : i === 80 ? null : 0))
    const points = downsample(time, values, 10)
    expect(points.length).toBeLessThanOrEqual(20)
    expect(points).toContainEqual([37, 50])
    expect(points.some(([t]) => t === 80)).toBe(false)
    expect(downsample([0, 1], [1, null], 10)).toEqual([[0, 1]])
  })
})
