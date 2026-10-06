/**
 * Zoom and pan over an image that may be far larger than the stage (IMG-3).
 *
 * The viewport is the only place image pixels and screen pixels meet. Every
 * shape stored in an `AnnotationResult` is in original image pixels (DATA-8);
 * conversion to and from stage coordinates happens here and nowhere else.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  computeFitViewport,
  constrainViewport,
  computeFocusViewport,
  getVisibleImageRect,
  toImageCoords,
  toStageCoords,
  type ImageRect,
  type Viewport,
} from './geometry'
import type { BBox, Point2D } from './types'

/** Multiplier per wheel notch. Small enough that a trackpad feels smooth. */
const ZOOM_STEP = 1.15

export interface UseViewportOptions {
  imageWidth: number
  imageHeight: number
  stageWidth: number
  stageHeight: number
  /** Fit this image rectangle instead of the whole image (IMG-6 region). */
  focus?: BBox | null
}

export interface UseViewportResult {
  viewport: Viewport
  /** Stage point -> original image pixels. */
  toImage: (point: Point2D) => Point2D
  /** Original image pixels -> stage point. */
  toStage: (point: Point2D) => Point2D
  /** Zoom toward a stage point, keeping the image pixel under it fixed. */
  zoomAt: (stagePoint: Point2D, factor: number) => void
  zoomIn: () => void
  zoomOut: () => void
  panBy: (dx: number, dy: number) => void
  fitToStage: () => void
  /** The image-space rectangle currently on screen, for virtualisation. */
  visibleRect: ImageRect
  /** True while space is held, so the stage should show a grab cursor and pan. */
  isPanning: boolean
  /** True once the view has been fitted to the current image and stage;
   * before that the viewport is a placeholder (scale 1 at the origin). */
  fitted: boolean
}

export function useViewport(options: UseViewportOptions): UseViewportResult {
  const { imageWidth, imageHeight, stageWidth, stageHeight, focus } = options
  // Every viewport is kept on the image: no zooming out past the fit, no
  // panning into empty space.
  const constrain = useCallback(
    (next: Viewport): Viewport =>
      constrainViewport(next, imageWidth, imageHeight, stageWidth, stageHeight),
    [imageWidth, imageHeight, stageWidth, stageHeight],
  )

  const fit = useCallback(
    (): Viewport =>
      constrain(
        focus
          ? computeFocusViewport(focus, stageWidth, stageHeight)
          : computeFitViewport(imageWidth, imageHeight, stageWidth, stageHeight),
      ),
    [constrain, focus, imageWidth, imageHeight, stageWidth, stageHeight],
  )

  const [rawViewport, setViewport] = useState<Viewport>(fit)
  // Re-applied on render so a resized stage cannot leave the image stranded.
  const viewport = useMemo(() => constrain(rawViewport), [constrain, rawViewport])
  const [isPanning, setIsPanning] = useState(false)

  // Refit when the image changes, or when the stage is first measured (its
  // initial size is 0 before layout).
  const fitKey = `${imageWidth}x${imageHeight}@${focus ? focus.join(',') : ''}`
  const lastFitKey = useRef<string | null>(null)
  const [fittedKey, setFittedKey] = useState<string | null>(null)
  useEffect(() => {
    if (stageWidth <= 0 || stageHeight <= 0) return
    if (lastFitKey.current === fitKey) return
    lastFitKey.current = fitKey
    setViewport(fit())
    setFittedKey(fitKey)
  }, [fitKey, fit, stageWidth, stageHeight])

  const toImage = useCallback(
    (point: Point2D): Point2D => toImageCoords(point, viewport),
    [viewport],
  )

  const toStage = useCallback(
    (point: Point2D): Point2D => toStageCoords(point, viewport),
    [viewport],
  )

  const zoomAt = useCallback(
    (stagePoint: Point2D, factor: number) => {
      setViewport((stored) => {
        const current = constrain(stored)
        const nextScale = constrain({ ...current, scale: current.scale * factor }).scale
        if (nextScale === current.scale) return current

        // Keep the image pixel under the cursor pinned to the same stage point:
        // solve toStage(imagePoint, next) === stagePoint for the new offset,
        // then let the edge constraint pull it back onto the image if needed.
        const imagePoint = toImageCoords(stagePoint, current)
        return constrain({
          scale: nextScale,
          offsetX: stagePoint[0] - imagePoint[0] * nextScale,
          offsetY: stagePoint[1] - imagePoint[1] * nextScale,
        })
      })
    },
    [constrain],
  )

  const zoomCentre = useCallback(
    (factor: number) => zoomAt([stageWidth / 2, stageHeight / 2], factor),
    [zoomAt, stageWidth, stageHeight],
  )

  const zoomIn = useCallback(() => zoomCentre(ZOOM_STEP), [zoomCentre])
  const zoomOut = useCallback(() => zoomCentre(1 / ZOOM_STEP), [zoomCentre])

  const panBy = useCallback(
    (dx: number, dy: number) => {
      setViewport((stored) => {
        const current = constrain(stored)
        return constrain({
          ...current,
          offsetX: current.offsetX + dx,
          offsetY: current.offsetY + dy,
        })
      })
    },
    [constrain],
  )

  const fitToStage = useCallback(() => setViewport(fit()), [fit])

  // Space held = pan mode. Tracked on the window so it survives focus moving
  // between the stage and the sidebars.
  useEffect(() => {
    const isTypingTarget = (target: EventTarget | null): boolean => {
      if (!(target instanceof HTMLElement)) return false
      return (
        target.isContentEditable ||
        ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)
      )
    }

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.code !== 'Space' || isTypingTarget(event.target)) return
      // Stop the page scrolling while the user pans.
      event.preventDefault()
      setIsPanning(true)
    }
    const onKeyUp = (event: KeyboardEvent) => {
      if (event.code === 'Space') setIsPanning(false)
    }
    // Releasing space outside the window never fires keyup; reset on blur.
    const onBlur = () => setIsPanning(false)

    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('keyup', onKeyUp)
    window.addEventListener('blur', onBlur)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('keyup', onKeyUp)
      window.removeEventListener('blur', onBlur)
    }
  }, [])

  const visibleRect = getVisibleImageRect(viewport, stageWidth, stageHeight)

  return {
    viewport,
    toImage,
    toStage,
    zoomAt,
    zoomIn,
    zoomOut,
    panBy,
    fitToStage,
    visibleRect,
    isPanning,
    fitted: fittedKey === fitKey,
  }
}
