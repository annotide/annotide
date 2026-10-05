import { describe, expect, it } from 'vitest'
import {
  clampBBoxToImage,
  clampPointToImage,
  clampScale,
  computeFitViewport,
  getVisibleImageRect,
  isDegenerateBBox,
  isShapeVisible,
  normalizeBBox,
  pointInPolygon,
  polygonArea,
  rboxCorners,
  rboxFromThreePoints,
  toImageCoords,
  toStageCoords,
  type Viewport,
} from './geometry'
import type { BBoxShape, PointShape, PolygonShape, RBoxShape } from './types'

describe('toStageCoords / toImageCoords round-trip', () => {
  const viewports: Viewport[] = [
    { scale: 1, offsetX: 0, offsetY: 0 },
    { scale: 2.5, offsetX: 100, offsetY: -40 },
    { scale: 0.1, offsetX: -500, offsetY: 300 },
    { scale: 40, offsetX: 0, offsetY: 0 },
    { scale: 0.02, offsetX: 1234.5, offsetY: -9876.25 },
  ]

  const points: Array<[number, number]> = [
    [0, 0],
    [1, 1],
    [123.456, 789.01],
    [-50, 200],
    [99999.9, 0.001],
  ]

  for (const viewport of viewports) {
    for (const point of points) {
      it(`round-trips ${JSON.stringify(point)} at scale=${viewport.scale} offset=(${viewport.offsetX},${viewport.offsetY})`, () => {
        const stage = toStageCoords(point, viewport)
        const back = toImageCoords(stage, viewport)
        expect(back[0]).toBeCloseTo(point[0], 6)
        expect(back[1]).toBeCloseTo(point[1], 6)
      })

      it(`is consistent the other way for ${JSON.stringify(point)}`, () => {
        const image = toImageCoords(point, viewport)
        const back = toStageCoords(image, viewport)
        expect(back[0]).toBeCloseTo(point[0], 6)
        expect(back[1]).toBeCloseTo(point[1], 6)
      })
    }
  }
})

describe('normalizeBBox', () => {
  it('normalises a top-left -> bottom-right drag', () => {
    expect(normalizeBBox([10, 20], [110, 220])).toEqual([10, 20, 110, 220])
  })

  it('normalises a bottom-right -> top-left drag', () => {
    expect(normalizeBBox([110, 220], [10, 20])).toEqual([10, 20, 110, 220])
  })

  it('normalises a top-right -> bottom-left drag', () => {
    expect(normalizeBBox([110, 20], [10, 220])).toEqual([10, 20, 110, 220])
  })

  it('normalises a bottom-left -> top-right drag', () => {
    expect(normalizeBBox([10, 220], [110, 20])).toEqual([10, 20, 110, 220])
  })
})

describe('clampBBoxToImage / clampPointToImage', () => {
  it('clamps a bbox that overshoots the image bounds', () => {
    expect(clampBBoxToImage([-50, -20, 5000, 6000], 1000, 800)).toEqual([0, 0, 1000, 800])
  })

  it('leaves an in-bounds bbox untouched', () => {
    expect(clampBBoxToImage([10, 10, 90, 90], 1000, 800)).toEqual([10, 10, 90, 90])
  })

  it('clamps a point outside the image', () => {
    expect(clampPointToImage([-5, 9999], 1000, 800)).toEqual([0, 800])
  })
})

describe('isDegenerateBBox', () => {
  it('rejects a box smaller than the default minimum size', () => {
    expect(isDegenerateBBox([0, 0, 1, 1])).toBe(true)
  })

  it('accepts a box at or above the minimum size', () => {
    expect(isDegenerateBBox([0, 0, 3, 3])).toBe(false)
    expect(isDegenerateBBox([0, 0, 10, 10])).toBe(false)
  })

  it('rejects a box that is thin in only one dimension', () => {
    expect(isDegenerateBBox([0, 0, 100, 1])).toBe(true)
  })

  it('honours a custom threshold', () => {
    expect(isDegenerateBBox([0, 0, 5, 5], 10)).toBe(true)
    expect(isDegenerateBBox([0, 0, 15, 15], 10)).toBe(false)
  })
})

describe('polygonArea', () => {
  it('computes the area of a unit square', () => {
    expect(polygonArea([[0, 0], [10, 0], [10, 10], [0, 10]])).toBe(100)
  })

  it('computes the area of a right triangle', () => {
    expect(
      polygonArea([
        [0, 0],
        [10, 0],
        [0, 10],
      ]),
    ).toBe(50)
  })

  it('is winding-direction independent', () => {
    const cw = polygonArea([[0, 0], [0, 10], [10, 10], [10, 0]])
    const ccw = polygonArea([[0, 0], [10, 0], [10, 10], [0, 10]])
    expect(cw).toBe(ccw)
  })

  it('returns 0 for fewer than 3 points', () => {
    expect(polygonArea([[0, 0], [1, 1]])).toBe(0)
    expect(polygonArea([])).toBe(0)
  })
})

describe('pointInPolygon', () => {
  const square: Array<[number, number]> = [
    [0, 0],
    [10, 0],
    [10, 10],
    [0, 10],
  ]

  it('detects a point well inside', () => {
    expect(pointInPolygon([5, 5], square)).toBe(true)
  })

  it('detects a point well outside', () => {
    expect(pointInPolygon([50, 50], square)).toBe(false)
  })

  it('detects a point outside along one axis', () => {
    expect(pointInPolygon([-5, 5], square)).toBe(false)
  })

  it('handles a concave polygon correctly', () => {
    const notch: Array<[number, number]> = [
      [0, 0],
      [10, 0],
      [10, 10],
      [5, 5],
      [0, 10],
    ]
    // Inside the notch (the V cut up from the bottom edge).
    expect(pointInPolygon([5, 8], notch)).toBe(false)
    // Left of the notch, clearly inside. Not [2, 8]: that lies exactly on the
    // (5,5)-(0,10) edge, and ray casting gives no meaningful answer on a
    // boundary — a test there pins arbitrary behaviour rather than intent.
    expect(pointInPolygon([2, 7], notch)).toBe(true)
  })
})

describe('clampScale', () => {
  it('clamps below the minimum', () => {
    expect(clampScale(0.0001)).toBe(0.02)
  })

  it('clamps above the maximum', () => {
    expect(clampScale(1000)).toBe(40)
  })

  it('leaves an in-range scale untouched', () => {
    expect(clampScale(1)).toBe(1)
  })

  it('falls back to the minimum for non-finite input', () => {
    expect(clampScale(NaN)).toBe(0.02)
    expect(clampScale(Infinity)).toBe(0.02)
  })
})

describe('computeFitViewport', () => {
  it('fits a wide image constrained by width', () => {
    const viewport = computeFitViewport(2000, 1000, 800, 800)
    expect(viewport.scale).toBeCloseTo(0.4, 5)
    expect(viewport.offsetX).toBeCloseTo(0, 5)
    expect(viewport.offsetY).toBeCloseTo(200, 5)
  })

  it('fits a tall image constrained by height', () => {
    const viewport = computeFitViewport(1000, 2000, 800, 800)
    expect(viewport.scale).toBeCloseTo(0.4, 5)
    expect(viewport.offsetY).toBeCloseTo(0, 5)
    expect(viewport.offsetX).toBeCloseTo(200, 5)
  })

  it('centers the image inside the stage', () => {
    const viewport = computeFitViewport(100, 100, 500, 500)
    expect(viewport.scale).toBe(1)
    expect(viewport.offsetX).toBe(200)
    expect(viewport.offsetY).toBe(200)
  })

  it('falls back to a sane default for degenerate input', () => {
    expect(computeFitViewport(0, 100, 500, 500)).toEqual({ scale: 1, offsetX: 0, offsetY: 0 })
  })
})

describe('getVisibleImageRect / isShapeVisible (virtualisation)', () => {
  const viewport: Viewport = { scale: 2, offsetX: -100, offsetY: -100 }
  // stage 800x600 -> image-space visible rect is [ (0+100)/2 .. (800+100)/2 ] etc.
  const rect = getVisibleImageRect(viewport, 800, 600)

  it('computes the visible image-space rectangle from the viewport', () => {
    expect(rect.x).toBeCloseTo(50, 5)
    expect(rect.y).toBeCloseTo(50, 5)
    expect(rect.width).toBeCloseTo(400, 5)
    expect(rect.height).toBeCloseTo(300, 5)
  })

  const inViewBBox: BBoxShape = {
    id: 'a',
    type: 'bbox',
    class: 'car',
    attributes: {},
    confidence: null,
    bbox: [100, 100, 150, 150],
  }
  const outOfViewBBox: BBoxShape = {
    id: 'b',
    type: 'bbox',
    class: 'car',
    attributes: {},
    confidence: null,
    bbox: [10000, 10000, 10010, 10010],
  }
  const inViewPolygon: PolygonShape = {
    id: 'c',
    type: 'polygon',
    class: 'road',
    attributes: {},
    confidence: null,
    points: [[60, 60], [70, 60], [70, 70]],
  }
  const inViewPoint: PointShape = {
    id: 'd',
    type: 'point',
    class: 'defect',
    attributes: {},
    confidence: null,
    point: [200, 200],
  }

  it('keeps shapes that intersect the visible rect', () => {
    expect(isShapeVisible(inViewBBox, rect)).toBe(true)
    expect(isShapeVisible(inViewPolygon, rect)).toBe(true)
    expect(isShapeVisible(inViewPoint, rect)).toBe(true)
  })

  it('culls shapes entirely outside the visible rect', () => {
    expect(isShapeVisible(outOfViewBBox, rect)).toBe(false)
  })
})

describe('rotated box geometry', () => {
  const round = (points: number[][]) => points.map((p) => p.map((v) => Math.round(v * 1000) / 1000))

  it('rboxCorners at angle 0 is the axis-aligned rectangle, clockwise from top-left', () => {
    expect(rboxCorners([10, 10], [4, 2], 0)).toEqual([
      [8, 9],
      [12, 9],
      [12, 11],
      [8, 11],
    ])
  })

  it('rboxCorners rotates clockwise on screen for a positive angle', () => {
    // 90° clockwise (y down): the box's +x axis points down the screen.
    expect(round(rboxCorners([50, 50], [40, 20], 90))).toEqual([
      [60, 30],
      [60, 70],
      [40, 70],
      [40, 30],
    ])
  })

  it('rboxFromThreePoints derives width from a→b, height from the perpendicular distance', () => {
    const geometry = rboxFromThreePoints([0, 0], [10, 0], [3, 4])
    expect(geometry).not.toBeNull()
    expect(geometry?.size).toEqual([10, 4])
    expect(geometry?.angle).toBe(0)
    expect(geometry?.center).toEqual([5, 2])
  })

  it('rboxFromThreePoints keeps the box on whichever side the third point falls', () => {
    const geometry = rboxFromThreePoints([0, 0], [10, 0], [3, -4])
    expect(geometry?.size).toEqual([10, 4])
    expect(geometry?.center).toEqual([5, -2])
  })

  it('rboxFromThreePoints round-trips through rboxCorners for a tilted side', () => {
    const a: [number, number] = [0, 0]
    const b: [number, number] = [30, 40] // length 50, angle atan2(40, 30) ≈ 53.13°
    const geometry = rboxFromThreePoints(a, b, [-8, 6]) // 10 px "above" the side
    expect(geometry?.size[0]).toBeCloseTo(50)
    expect(geometry?.size[1]).toBeCloseTo(10)
    expect(geometry?.angle).toBeCloseTo(53.13, 1)
    const corners = round(rboxCorners(geometry!.center, geometry!.size, geometry!.angle))
    // The clicked side a→b is one edge of the resulting box.
    expect(corners).toContainEqual([0, 0])
    expect(corners).toContainEqual([30, 40])
  })

  it('rboxFromThreePoints is null while the first side has no length', () => {
    expect(rboxFromThreePoints([5, 5], [5, 5], [9, 9])).toBeNull()
  })

  it('isShapeVisible uses the rotated envelope for rbox', () => {
    const shape: RBoxShape = {
      id: 'r',
      type: 'rbox',
      class: 'ship',
      attributes: {},
      confidence: null,
      center: [50, 50],
      size: [40, 20],
      angle: 90,
    }
    // On screen the box spans x 40..60, y 30..70.
    expect(isShapeVisible(shape, { x: 0, y: 0, width: 45, height: 35 })).toBe(true)
    expect(isShapeVisible(shape, { x: 0, y: 0, width: 35, height: 35 })).toBe(false)
  })
})
