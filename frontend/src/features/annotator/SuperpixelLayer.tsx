/**
 * The superpixel tool's canvas side: computing the segmentation from the
 * loaded image, and drawing its boundaries plus the superpixels a gesture has
 * picked (and the one under the pointer). The pure parts are `superpixels.ts`.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import type Konva from 'konva'
import { Image as KonvaImage } from 'react-konva'

import { hexToRgb } from './mask'
import { boundaryPixels, segment, superpixelPixels, workingSize } from './superpixels'
import type { Segmentation } from './superpixels'

export type SuperpixelStatus = 'idle' | 'computing' | 'ready' | 'error'

const BOUNDARY_RGBA: [number, number, number, number] = [255, 255, 255, 150]
const PICK_ALPHA = 140

/**
 * Segments `image` once `enabled` (the tool is active), keeping the result
 * per `mediaKey` and segment count so switching tools does not redo it, nor
 * does a fresh signature for the same object (a refetch re-signs the URL and
 * loads a new element). Reading the pixels needs the storage to allow CORS
 * for the signed URL; a tainted canvas ends in `error`.
 */
export function useSuperpixels(
  image: HTMLImageElement | null,
  mediaKey: string,
  imageWidth: number,
  imageHeight: number,
  enabled: boolean,
  segments: number,
): { segmentation: Segmentation | null; status: SuperpixelStatus } {
  const cache = useRef<{ mediaKey: string; segments: number; seg: Segmentation } | null>(null)
  const [segmentation, setSegmentation] = useState<Segmentation | null>(null)
  const [status, setStatus] = useState<SuperpixelStatus>('idle')

  useEffect(() => {
    if (!enabled) return
    const hit = cache.current
    if (hit && hit.mediaKey === mediaKey && hit.segments === segments) {
      setSegmentation(hit.seg)
      setStatus('ready')
      return
    }
    if (!image) return
    setSegmentation(null)
    setStatus('computing')
    // Yield first so "Computing superpixels…" paints before the busy pass.
    const timer = window.setTimeout(() => {
      try {
        const size = workingSize(imageWidth, imageHeight)
        const canvas = document.createElement('canvas')
        canvas.width = size.width
        canvas.height = size.height
        const context = canvas.getContext('2d', { willReadFrequently: true })
        if (!context) throw new Error('no 2d context')
        context.drawImage(image, 0, 0, size.width, size.height)
        const { data } = context.getImageData(0, 0, size.width, size.height)
        const seg = segment(data, size.width, size.height, size.scale, segments)
        cache.current = { mediaKey, segments, seg }
        setSegmentation(seg)
        setStatus('ready')
      } catch {
        setStatus('error')
      }
    }, 30)
    return () => window.clearTimeout(timer)
  }, [enabled, image, imageHeight, imageWidth, mediaKey, segments])

  return { segmentation: enabled ? segmentation : null, status: enabled ? status : 'idle' }
}

function canvasOf(width: number, height: number): HTMLCanvasElement {
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  return canvas
}

export interface SuperpixelOverlayProps {
  segmentation: Segmentation
  imageWidth: number
  imageHeight: number
  /** Superpixels to fill: the gesture's picks and the hovered one. */
  picked: number[]
  color: string
}

/** Boundaries and picks, stretched from the working size over the image. */
export function SuperpixelOverlay({
  segmentation,
  imageWidth,
  imageHeight,
  picked,
  color,
}: SuperpixelOverlayProps) {
  const boundaries = useMemo(() => {
    const canvas = canvasOf(segmentation.width, segmentation.height)
    const data = boundaryPixels(segmentation, BOUNDARY_RGBA)
    canvas
      .getContext('2d')
      ?.putImageData(new ImageData(data, segmentation.width, segmentation.height), 0, 0)
    return canvas
  }, [segmentation])

  const picks = useMemo(
    () => canvasOf(segmentation.width, segmentation.height),
    [segmentation],
  )
  const picksRef = useRef<Konva.Image>(null)
  useEffect(() => {
    const context = picks.getContext('2d')
    if (!context) return
    context.clearRect(0, 0, picks.width, picks.height)
    const [r, g, b] = hexToRgb(color)
    for (const label of picked) {
      if (label < 0 || label >= segmentation.count) continue
      const { x, y, width, height, data } = superpixelPixels(segmentation, label, [
        r,
        g,
        b,
        PICK_ALPHA,
      ])
      // putImageData replaces pixels, transparent ones included; draw through
      // a scratch canvas so neighbouring picks' bounds do not erase each other.
      const scratch = canvasOf(width, height)
      scratch.getContext('2d')?.putImageData(new ImageData(data, width, height), 0, 0)
      context.drawImage(scratch, x, y)
    }
    picksRef.current?.getLayer()?.batchDraw()
  }, [color, picked, picks, segmentation])

  return (
    <>
      <KonvaImage
        image={boundaries}
        width={imageWidth}
        height={imageHeight}
        listening={false}
      />
      <KonvaImage
        ref={picksRef}
        image={picks}
        width={imageWidth}
        height={imageHeight}
        listening={false}
      />
    </>
  )
}
