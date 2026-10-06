/** Pure helpers for relations between pdf shapes (CONTRACTS.md "PDF items"). */
import type { Shape } from '@/api/types'

export type Point = [number, number]

/** Where a relation's arrow attaches to a shape, in page points; null if it has no anchor. */
export function shapeAnchor(shape: Shape): Point | null {
  switch (shape.type) {
    case 'span': {
      const box = shape.boxes?.[0]
      return box ? [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2] : null
    }
    case 'bbox':
      return [(shape.bbox[0] + shape.bbox[2]) / 2, (shape.bbox[1] + shape.bbox[3]) / 2]
    case 'polygon':
    case 'polyline': {
      const n = shape.points.length
      if (n === 0) return null
      return [
        shape.points.reduce((sum, p) => sum + p[0], 0) / n,
        shape.points.reduce((sum, p) => sum + p[1], 0) / n,
      ]
    }
    case 'point':
      return shape.point
    default:
      return null
  }
}

/** Triangle points for an arrow head at `to`, pointing away from `from`. */
export function arrowHead(from: Point, to: Point, size: number): string {
  const angle = Math.atan2(to[1] - from[1], to[0] - from[0])
  const wing = (offset: number): Point => [
    to[0] - size * Math.cos(angle + offset),
    to[1] - size * Math.sin(angle + offset),
  ]
  const [a, b] = [wing(0.45), wing(-0.45)]
  return `${to[0]},${to[1]} ${a[0]},${a[1]} ${b[0]},${b[1]}`
}
