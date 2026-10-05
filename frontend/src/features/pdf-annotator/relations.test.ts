import { describe, expect, it } from 'vitest'
import type { Shape } from '@/api/types'
import { arrowHead, shapeAnchor } from './relations'

const base = { id: 'a', class: 'c', attributes: {}, confidence: null, page: 1 }

describe('shapeAnchor', () => {
  it('uses the centre of a box and of the first box of a span', () => {
    expect(shapeAnchor({ ...base, type: 'bbox', bbox: [0, 0, 10, 20] } as Shape)).toEqual([5, 10])
    expect(
      shapeAnchor({ ...base, type: 'span', boxes: [[10, 10, 30, 20], [0, 30, 5, 40]] } as Shape),
    ).toEqual([20, 15])
  })
  it('has none for a relation', () => {
    expect(shapeAnchor({ ...base, type: 'relation', from: 'x', to: 'y' } as Shape)).toBeNull()
  })
})

describe('arrowHead', () => {
  it('has its tip at the target', () => {
    expect(arrowHead([0, 0], [10, 0], 4).startsWith('10,0 ')).toBe(true)
  })
})
