/**
 * Konva nodes for a Deep Zoom (DZI) tile pyramid (IMG-1). Meant to sit inside
 * the annotator's media `Layer`, which already carries the viewport's
 * x/y/scale, so tiles are placed in original-image pixel coordinates like
 * everything else on that layer — the whole image is never loaded.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Image as KonvaImage } from 'react-konva'

import type { ImageRect } from './geometry'
import { levelForScale, tileKey, visibleTiles, type VisibleTile } from './tiles'
import type { ItemTiles, TileKey } from './types'

/** Loaded tile images kept around across viewport moves, bounded so panning
 * a very large image cannot grow memory without limit. */
const CACHE_LIMIT = 512

export interface TiledImageProps {
  meta: ItemTiles
  /** Viewport scale (image px -> screen px); picks the tile level. */
  scale: number
  /** The image-space rectangle currently on screen (IMG-4 style virtualisation). */
  visibleRect: ImageRect
  /** Signs a batch (<=512) of `[level, col, row]` tiles and resolves to their
   * signed read URLs, in the same order (IMG-1). */
  signTiles: (tiles: TileKey[]) => Promise<string[]>
}

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new window.Image()
    img.crossOrigin = 'anonymous'
    img.onload = () => resolve(img)
    img.onerror = () => reject(new Error('tile failed to load'))
    img.src = url
  })
}

function withTile(
  cache: Map<string, HTMLImageElement>,
  key: string,
  img: HTMLImageElement,
): Map<string, HTMLImageElement> {
  const next = new Map(cache)
  next.delete(key)
  next.set(key, img)
  while (next.size > CACHE_LIMIT) {
    const oldest = next.keys().next().value
    if (oldest === undefined) break
    next.delete(oldest)
  }
  return next
}

export function TiledImage({ meta, scale, visibleRect, signTiles }: TiledImageProps) {
  const [loaded, setLoaded] = useState<Map<string, HTMLImageElement>>(new Map())
  const pendingRef = useRef<Set<string>>(new Set())
  const retriesRef = useRef<Map<string, number>>(new Map())
  const mountedRef = useRef(true)
  useEffect(
    () => () => {
      mountedRef.current = false
    },
    [],
  )

  const level = levelForScale(scale, meta.max_level)
  const fallbackLevel = Math.max(0, level - 1)
  const { x, y, width, height } = visibleRect

  // Coarser tiles are drawn first and kept on screen until the finer ones
  // for the same area finish loading, so zooming/panning never shows a gap.
  const fallbackTiles = useMemo(
    () => (fallbackLevel === level ? [] : visibleTiles(meta, fallbackLevel, { x, y, width, height })),
    [meta, fallbackLevel, level, x, y, width, height],
  )
  const tiles = useMemo(
    () => visibleTiles(meta, level, { x, y, width, height }),
    [meta, level, x, y, width, height],
  )

  useEffect(() => {
    const wanted = [...fallbackTiles, ...tiles]
    const needed = wanted.filter((tile) => {
      const key = tileKey(tile)
      return !loaded.has(key) && !pendingRef.current.has(key)
    })
    if (needed.length === 0) return

    const keys = needed.map(tileKey)
    for (const key of keys) pendingRef.current.add(key)

    signTiles(needed.map((t): TileKey => [t.level, t.col, t.row]))
      .then((urls) =>
        Promise.allSettled(urls.map((url) => loadImage(url))).then((results) =>
          results.map((result, index) => ({ key: keys[index], tile: needed[index], result })),
        ),
      )
      .then((settled) => {
        if (!mountedRef.current) return
        const succeeded: { key: string; img: HTMLImageElement }[] = []
        for (const { key, tile, result } of settled) {
          pendingRef.current.delete(key)
          if (result.status === 'fulfilled') {
            succeeded.push({ key, img: result.value })
            continue
          }
          // A signed URL that expired before the load finished: sign once
          // more instead of leaving the tile permanently blank.
          const retries = retriesRef.current.get(key) ?? 0
          if (retries >= 1) continue
          retriesRef.current.set(key, retries + 1)
          signTiles([[tile.level, tile.col, tile.row]])
            .then(([url]) => (url ? loadImage(url) : Promise.reject(new Error('no url'))))
            .then((img) => {
              if (!mountedRef.current) return
              setLoaded((prev) => withTile(prev, key, img))
            })
            .catch(() => undefined)
        }
        if (succeeded.length > 0) {
          setLoaded((prev) => {
            let next = prev
            for (const { key, img } of succeeded) next = withTile(next, key, img)
            return next
          })
        }
      })
      .catch(() => {
        for (const key of keys) pendingRef.current.delete(key)
      })
    // `loaded` drives what still needs fetching but must not retrigger this
    // effect on every tile arrival — only the wanted tile set matters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tiles, fallbackTiles, signTiles])

  const renderTile = (tile: VisibleTile, keyPrefix: string) => {
    const img = loaded.get(tileKey(tile))
    if (!img) return null
    return (
      <KonvaImage
        key={`${keyPrefix}${tileKey(tile)}`}
        image={img}
        x={tile.x}
        y={tile.y}
        width={tile.w}
        height={tile.h}
        listening={false}
      />
    )
  }

  return (
    <>
      {fallbackTiles.map((tile) => renderTile(tile, 'fallback-'))}
      {tiles.map((tile) => renderTile(tile, ''))}
    </>
  )
}
