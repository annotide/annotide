/**
 * DZI tile pyramid wiring (IMG-1): when `tiles` is set, the media layer must
 * never request the whole image, and must sign the tiles the initial
 * viewport needs. Konva itself is stubbed (see ImageAnnotator.test.tsx for
 * the rationale); this file adds a real-sized stage so the media Layer
 * actually mounts, unlike the zero-size stage used by the other suite.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ImageAnnotator } from './ImageAnnotator'
import type { AnnotationResult, ItemTiles, LabelClass, TileKey } from './types'

vi.mock('react-konva', async () => {
  const React = await import('react')
  const passthrough =
    (tag: string) =>
    ({ children }: { children?: React.ReactNode }) =>
      React.createElement(tag === 'stage' ? 'div' : 'span', { 'data-konva': tag }, children)
  return {
    Stage: passthrough('stage'),
    Layer: passthrough('layer'),
    Image: ({ image, x, y, width, height }: { image?: HTMLImageElement } & Record<string, unknown>) =>
      React.createElement('img', {
        'data-testid': 'konva-image',
        'data-x': String(x),
        'data-y': String(y),
        'data-w': String(width),
        'data-h': String(height),
        src: image?.src ?? '',
        alt: '',
      }),
    Line: passthrough('line'),
    Rect: passthrough('rect'),
    Circle: passthrough('circle'),
    Group: passthrough('group'),
    Text: passthrough('text'),
  }
})

/** Fires immediately with a real size, so the stage's `Stage`/`Layer`s mount
 * (the other suite's stub never does, which is fine there but useless here). */
class ImmediateResizeObserver {
  private readonly callback: (entries: unknown) => void
  constructor(callback: (entries: unknown) => void) {
    this.callback = callback
  }
  observe(): void {
    this.callback([{ contentRect: { width: 800, height: 600 } }])
  }
  unobserve(): void {}
  disconnect(): void {}
}

/** `src` values ever assigned to a `new Image()`, across both the (skipped)
 * full-image load and every tile load. */
let requestedSrcs: string[] = []

class MockImage {
  crossOrigin = ''
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  private _src = ''
  set src(value: string) {
    this._src = value
    requestedSrcs.push(value)
    queueMicrotask(() => this.onload?.())
  }
  get src(): string {
    return this._src
  }
}

const CLASSES: LabelClass[] = [
  { name: 'cat', display_name: 'Cat', color: '#ff0000', tools: ['bbox', 'mask'], attributes: [] },
]

const EMPTY: AnnotationResult = {
  schema_version: 1,
  media_type: 'image',
  classification: {},
  shapes: [],
}

/** 2000x1500 px so the fit-to-stage scale (800x600) is exactly 0.4 on both
 * axes — the viewport then covers the whole image with no letterboxing,
 * which keeps the expected tile set simple to state. max_level = 11. */
const TILES: ItemTiles = {
  format: 'dzi',
  path: 'cache/tiles/item-1/',
  tile_size: 256,
  overlap: 0,
  suffix: 'jpeg',
  max_level: 11,
  width: 2000,
  height: 1500,
}

describe('ImageAnnotator with a DZI tile pyramid (IMG-1)', () => {
  beforeEach(() => {
    requestedSrcs = []
    vi.stubGlobal('Image', MockImage)
    vi.stubGlobal('ResizeObserver', ImmediateResizeObserver)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('never requests the full image and signs the tiles the initial viewport needs', async () => {
    const signTiles = vi.fn((tiles: TileKey[]) =>
      Promise.resolve(tiles.map(([level, col, row]) => `https://signed.example/${level}/${col}_${row}`)),
    )

    render(
      <ImageAnnotator
        imageUrl="https://storage.example/huge.jpg?sig=abc"
        imageWidth={TILES.width}
        imageHeight={TILES.height}
        classes={CLASSES}
        value={EMPTY}
        onChange={() => {}}
        tiles={TILES}
        itemId="item-1"
        signTiles={signTiles}
      />,
    )

    await waitFor(() => expect(signTiles).toHaveBeenCalledTimes(1))
    // A tiled image is far too large to hold as a mask bitmap.
    expect(screen.queryByRole('button', { name: /^Brush/ })).toBeNull()

    const requested = signTiles.mock.calls[0][0] as TileKey[]
    // Level 10 (fit scale 0.4) covers the whole 2000x1500 image in a 4x3 grid.
    const level10 = requested.filter(([level]) => level === 10)
    expect(level10).toEqual(
      expect.arrayContaining([
        [10, 0, 0],
        [10, 3, 2],
      ]),
    )
    expect(level10).toHaveLength(12)
    // The coarser fallback level (9) is requested alongside it.
    expect(requested.some(([level]) => level === 9)).toBe(true)

    await waitFor(() => expect(screen.getAllByTestId('konva-image').length).toBeGreaterThan(0))

    expect(requestedSrcs).not.toContain('https://storage.example/huge.jpg?sig=abc')
    expect(requestedSrcs.some((src) => src.startsWith('https://signed.example/'))).toBe(true)
  })

  it('does not construct an Image for the full media URL at all when tiles is set', async () => {
    const signTiles = vi.fn(() => Promise.resolve<string[]>([]))
    render(
      <ImageAnnotator
        imageUrl="https://storage.example/huge.jpg?sig=abc"
        imageWidth={TILES.width}
        imageHeight={TILES.height}
        classes={CLASSES}
        value={EMPTY}
        onChange={() => {}}
        tiles={TILES}
        itemId="item-1"
        signTiles={signTiles}
      />,
    )

    await waitFor(() => expect(signTiles).toHaveBeenCalled())
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
