/**
 * Pointer math for the video overlay: the SVG's viewBox is the image's own
 * pixel space (`0 0 width height`), scaled to whatever size it renders at —
 * so a client coordinate has to be rescaled by the element's own client rect
 * before it means anything as an image pixel.
 */
import type { BBox } from '@/api/types'

export interface ClientRectLike {
  left: number
  top: number
  width: number
  height: number
}

/** Converts a client-space point (e.g. `MouseEvent.clientX/clientY`) to image pixels. */
export function clientToImagePoint(
  rect: ClientRectLike,
  imageWidth: number,
  imageHeight: number,
  clientX: number,
  clientY: number,
): [number, number] {
  const scaleX = rect.width > 0 ? imageWidth / rect.width : 1
  const scaleY = rect.height > 0 ? imageHeight / rect.height : 1
  return [(clientX - rect.left) * scaleX, (clientY - rect.top) * scaleY]
}

/** A bbox from two dragged corners, always ordered min → max. */
export function bboxFromCorners(a: [number, number], b: [number, number]): BBox {
  return [Math.min(a[0], b[0]), Math.min(a[1], b[1]), Math.max(a[0], b[0]), Math.max(a[1], b[1])]
}

/** Translates a bbox by the delta between two points (a move drag). */
export function translateBBox(bbox: BBox, from: [number, number], to: [number, number]): BBox {
  const dx = to[0] - from[0]
  const dy = to[1] - from[1]
  return [bbox[0] + dx, bbox[1] + dy, bbox[2] + dx, bbox[3] + dy]
}
