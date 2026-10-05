import { describe, expect, it } from 'vitest'

import { dragHandle, removeVertex, shapeHandles, translateShape } from './edit'
import { rboxCorners } from './geometry'
import type { Shape } from './types'

const base = { id: 's1', class: 'car', attributes: {}, confidence: null }
const W = 1000
const H = 800

const bbox: Shape = { ...base, type: 'bbox', bbox: [100, 100, 200, 150] }
const rbox: Shape = { ...base, type: 'rbox', center: [500, 400], size: [100, 40], angle: 0 }
const polygon: Shape = {
  ...base,
  type: 'polygon',
  points: [
    [10, 10],
    [50, 10],
    [50, 50],
  ],
}
const polyline: Shape = {
  ...base,
  type: 'polyline',
  points: [
    [10, 10],
    [50, 10],
  ],
}
const keypoints: Shape = {
  ...base,
  type: 'keypoints',
  points: [
    [10, 10, 2],
    [0, 0, 0],
    [30, 30, 1],
  ],
}

describe('shapeHandles', () => {
  it('gives a box its corners then edge midpoints', () => {
    const handles = shapeHandles(bbox, 1)
    expect(handles.map((h) => h.point)).toEqual([
      [100, 100],
      [200, 100],
      [200, 150],
      [100, 150],
      [150, 100],
      [200, 125],
      [150, 150],
      [100, 125],
    ])
  })

  it('puts the rbox rotation handle a fixed screen distance above the box', () => {
    const rotate = shapeHandles(rbox, 2).find((h) => h.kind === 'rotate')
    expect(rotate?.point).toEqual([500, 400 - 20 - 12])
  })

  it('adds an insert handle per edge: closing edge for polygons only', () => {
    expect(shapeHandles(polygon, 1).filter((h) => h.kind === 'insert')).toHaveLength(3)
    expect(shapeHandles(polyline, 1).filter((h) => h.kind === 'insert')).toHaveLength(1)
  })

  it('skips unlabelled keypoints but keeps slot indices', () => {
    expect(shapeHandles(keypoints, 1).map((h) => h.index)).toEqual([0, 2])
  })

  it('gives points and masks no handles', () => {
    const point: Shape = { ...base, type: 'point', point: [1, 1] }
    expect(shapeHandles(point, 1)).toEqual([])
  })
})

describe('translateShape', () => {
  it('moves every coordinate', () => {
    expect(translateShape(bbox, 10, -20, W, H)).toMatchObject({ bbox: [110, 80, 210, 130] })
    expect(translateShape(rbox, 5, 5, W, H)).toMatchObject({ center: [505, 405] })
  })

  it('stops at the image edge without deforming the shape', () => {
    expect(translateShape(bbox, -500, 5000, W, H)).toMatchObject({ bbox: [0, 750, 100, 800] })
  })

  it('leaves unlabelled keypoint slots at their placeholder', () => {
    const moved = translateShape(keypoints, 5, 5, W, H)
    expect(moved).toMatchObject({
      points: [
        [15, 15, 2],
        [0, 0, 0],
        [35, 35, 1],
      ],
    })
  })

  it('refuses masks', () => {
    const mask: Shape = { ...base, type: 'mask', rle: { size: [2, 2], counts: [0, 4] } }
    expect(translateShape(mask, 1, 1, W, H)).toBeNull()
  })
})

describe('dragHandle', () => {
  it('resizes a box from a corner with the opposite corner fixed, and flips past it', () => {
    expect(dragHandle(bbox, 2, [300, 300], W, H)).toMatchObject({ bbox: [100, 100, 300, 300] })
    expect(dragHandle(bbox, 2, [50, 50], W, H)).toMatchObject({ bbox: [50, 50, 100, 100] })
  })

  it('moves one side from an edge handle', () => {
    expect(dragHandle(bbox, 5, [400, 999], W, H)).toMatchObject({ bbox: [100, 100, 400, 150] })
    expect(dragHandle(bbox, 4, [0, 20], W, H)).toMatchObject({ bbox: [100, 20, 200, 150] })
  })

  it('refuses a degenerate box and clamps to the image', () => {
    expect(dragHandle(bbox, 5, [101, 0], W, H)).toBeNull()
    expect(dragHandle(bbox, 2, [5000, 5000], W, H)).toMatchObject({ bbox: [100, 100, W, H] })
  })

  it('resizes a rotated box keeping the opposite corner and angle', () => {
    const turned: Shape = { ...base, type: 'rbox', center: [500, 400], size: [100, 40], angle: 30 }
    const corners = rboxCorners(turned.center, turned.size, turned.angle)
    const target = rboxCorners(turned.center, [200, 80], turned.angle)
    // Move corner 2 so the box doubles about corner 0.
    const far: [number, number] = [
      corners[0][0] + (target[2][0] - target[0][0]),
      corners[0][1] + (target[2][1] - target[0][1]),
    ]
    const next = dragHandle(turned, 2, far, W, H)
    if (next?.type !== 'rbox') throw new Error('expected an rbox')
    expect(next.angle).toBe(30)
    expect(next.size[0]).toBeCloseTo(200)
    expect(next.size[1]).toBeCloseTo(80)
    const after = rboxCorners(next.center, next.size, next.angle)
    expect(after[0][0]).toBeCloseTo(corners[0][0])
    expect(after[0][1]).toBeCloseTo(corners[0][1])
  })

  it('rotates an rbox towards the pointer', () => {
    expect(dragHandle(rbox, 4, [600, 400], W, H)).toMatchObject({ angle: 90 })
    expect(dragHandle(rbox, 4, [500, 500], W, H)).toMatchObject({ angle: 180 })
    expect(dragHandle(rbox, 4, [400, 400], W, H)).toMatchObject({ angle: -90 })
  })

  it('moves and inserts polygon vertices', () => {
    expect(dragHandle(polygon, 1, [60, 5], W, H)).toMatchObject({
      points: [
        [10, 10],
        [60, 5],
        [50, 50],
      ],
    })
    // Insert handle of edge 2 (the closing edge, 50,50 → 10,10).
    const inserted = dragHandle(polygon, 3 + 2, [20, 40], W, H)
    expect(inserted).toMatchObject({
      points: [
        [10, 10],
        [50, 10],
        [50, 50],
        [20, 40],
      ],
    })
  })

  it('moves a keypoint and keeps its visibility', () => {
    expect(dragHandle(keypoints, 2, [40, 44], W, H)).toMatchObject({
      points: [
        [10, 10, 2],
        [0, 0, 0],
        [40, 44, 1],
      ],
    })
    expect(dragHandle(keypoints, 1, [40, 44], W, H)).toBeNull()
  })
})

describe('removeVertex', () => {
  it('drops a polygon vertex but never below three', () => {
    const square: Shape = {
      ...base,
      type: 'polygon',
      points: [
        [0, 0],
        [1, 0],
        [1, 1],
        [0, 1],
      ],
    }
    const triangle = removeVertex(square, 0)
    expect(triangle).toMatchObject({ points: [[1, 0], [1, 1], [0, 1]] })
    expect(triangle && removeVertex(triangle, 0)).toBeNull()
    expect(removeVertex(polyline, 0)).toBeNull()
  })

  it('unlabels a keypoint rather than removing its slot, keeping one labelled', () => {
    const one = removeVertex(keypoints, 0)
    expect(one).toMatchObject({
      points: [
        [0, 0, 0],
        [0, 0, 0],
        [30, 30, 1],
      ],
    })
    expect(one && removeVertex(one, 2)).toBeNull()
  })
})
