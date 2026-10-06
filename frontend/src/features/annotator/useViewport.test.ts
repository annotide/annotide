import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { VIEWPORT_EDGE_MARGIN_PX } from './geometry'
import { useViewport } from './useViewport'

// A 2000x1000 image on an 800x800 stage fits at scale 0.4, centred vertically.
const OPTIONS = { imageWidth: 2000, imageHeight: 1000, stageWidth: 800, stageHeight: 800 }

describe('useViewport', () => {
  it('does not zoom out past the fitted image', () => {
    const { result } = renderHook(() => useViewport(OPTIONS))
    const fitted = result.current.viewport
    act(() => {
      for (let i = 0; i < 10; i++) result.current.zoomOut()
    })
    expect(result.current.viewport).toEqual(fitted)
  })

  it('does not pan the fitted image off the stage', () => {
    const { result } = renderHook(() => useViewport(OPTIONS))
    const fitted = result.current.viewport
    act(() => result.current.panBy(300, -300))
    expect(result.current.viewport).toEqual(fitted)
  })

  it('pans a zoomed image only as far as its edge', () => {
    const { result } = renderHook(() => useViewport(OPTIONS))
    act(() => result.current.zoomAt([0, 0], 2.5)) // scale 1: 2000x1000 on screen
    act(() => result.current.panBy(5000, 5000))
    expect(result.current.viewport).toEqual({
      scale: 1,
      offsetX: VIEWPORT_EDGE_MARGIN_PX,
      offsetY: VIEWPORT_EDGE_MARGIN_PX,
    })
    // Panning back responds at once: the overshoot was not stored.
    act(() => result.current.panBy(-10, -10))
    expect(result.current.viewport.offsetX).toBe(VIEWPORT_EDGE_MARGIN_PX - 10)
  })

  it('pulls the image back when the stage grows', () => {
    const { result, rerender } = renderHook((props) => useViewport(props), {
      initialProps: OPTIONS,
    })
    act(() => result.current.zoomAt([0, 0], 2.5))
    act(() => result.current.panBy(-5000, 0))
    rerender({ ...OPTIONS, stageWidth: 1600 })
    expect(result.current.viewport.offsetX).toBe(1600 - 2000 - VIEWPORT_EDGE_MARGIN_PX)
  })
})
