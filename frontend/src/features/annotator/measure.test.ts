import { describe, expect, it } from 'vitest'
import { describeShape, length, PIXELS, resolveScale } from './measure'
import type { PixelScale } from './measure'
import type { Shape } from './types'

const MM: PixelScale = { x: 0.5, y: 0.5, unit: 'mm', source: 'project' }
const base = { id: 's', class: 'c', attributes: {}, confidence: null }

describe('resolveScale', () => {
  const project = { calibration: { units_per_pixel: 0.5, unit: 'mm' } }

  it('prefers the item, then the project, then pixels', () => {
    const item = { pixel_spacing: { x: 0.1, y: 0.2, unit: 'mm' } }
    expect(resolveScale(item, project)).toEqual({ x: 0.1, y: 0.2, unit: 'mm', source: 'item' })
    expect(resolveScale({}, project)).toEqual(MM)
    expect(resolveScale({}, {})).toBe(PIXELS)
    expect(resolveScale(undefined, undefined)).toBe(PIXELS)
  })

  it('ignores malformed values', () => {
    expect(resolveScale({ pixel_spacing: { x: 0, y: 1, unit: 'mm' } }, project)).toEqual(MM)
    expect(resolveScale({ pixel_spacing: { x: 1, y: 1 } }, project)).toEqual(MM)
    expect(resolveScale({}, { calibration: { units_per_pixel: -1, unit: 'mm' } })).toBe(PIXELS)
    expect(resolveScale({}, { calibration: { units_per_pixel: 1, unit: '' } })).toBe(PIXELS)
  })
})

describe('measurements', () => {
  it('measures per axis when spacing differs', () => {
    const scale: PixelScale = { x: 2, y: 1, unit: 'm', source: 'item' }
    expect(length([0, 0], [3, 4], PIXELS)).toBe(5)
    expect(length([0, 0], [3, 4], scale)).toBeCloseTo(Math.hypot(6, 4))
  })

  it('describes each measurable shape', () => {
    const shapes: Shape[] = [
      { ...base, type: 'bbox', bbox: [10, 10, 30, 50] },
      { ...base, type: 'rbox', center: [0, 0], size: [20, 10], angle: 30 },
      { ...base, type: 'polygon', points: [[0, 0], [10, 0], [10, 10], [0, 10]] },
      { ...base, type: 'polyline', points: [[0, 0], [3, 4], [3, 10]] },
      { ...base, type: 'point', point: [1, 1] },
    ]
    expect(shapes.map((shape) => describeShape(shape, MM))).toEqual([
      '10 × 20 mm',
      '10 × 5 mm',
      '25 mm², perimeter 20 mm',
      '5.5 mm',
      null,
    ])
    expect(describeShape(shapes[0], PIXELS)).toBe('20 × 40 px')
  })
})
