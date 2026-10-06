/**
 * Deep Zoom (DZI) tile pyramid geometry (IMG-1, docs/CONTRACTS.md "tile_image"
 * and "POST /items/{id}/tiles/sign"). Pure functions only — no Konva, no
 * network — so a very large image can be shown tile by tile without the
 * browser ever loading the whole thing.
 *
 * Levels: level 0 is 1x1 px, level `max_level` is the full image; level L is
 * `ceil(W / 2^(max_level - L)) x ceil(H / 2^(max_level - L))`. A level-L tile
 * covers `tile_size * 2^(max_level - L)` original-image pixels per side.
 */

import type { ImageRect } from './geometry'
import type { ItemTiles } from './types'

/** One tile's placement, in ORIGINAL image pixels. */
export interface VisibleTile {
  level: number
  col: number
  row: number
  x: number
  y: number
  w: number
  h: number
}

/** Above this many tiles for one viewport, fall back to a coarser level. */
export const MAX_VISIBLE_TILES = 256

/** `{level}/{col}_{row}`, matching the tile path on the result connector. */
export function tileKey(tile: { level: number; col: number; row: number }): string {
  return `${tile.level}/${tile.col}_${tile.row}`
}

/**
 * The smallest level whose resolution covers what the viewport shows at
 * `scale` (image px -> screen px). At `scale >= 1` (zoomed in past 1:1) that
 * is always `maxLevel`; zoomed out, coarser levels have enough resolution.
 */
export function levelForScale(scale: number, maxLevel: number): number {
  if (!(scale > 0)) return 0
  const level = maxLevel - Math.floor(Math.log2(1 / scale))
  return Math.min(maxLevel, Math.max(0, level))
}

/** The pixel dimensions of one level of the pyramid. */
export function levelSize(meta: ItemTiles, level: number): { width: number; height: number } {
  const factor = 2 ** (meta.max_level - level)
  return {
    width: Math.max(1, Math.ceil(meta.width / factor)),
    height: Math.max(1, Math.ceil(meta.height / factor)),
  }
}

/** The tile grid dimensions (columns x rows) of one level. */
export function tileGridSize(meta: ItemTiles, level: number): { cols: number; rows: number } {
  const { width, height } = levelSize(meta, level)
  return {
    cols: Math.ceil(width / meta.tile_size),
    rows: Math.ceil(height / meta.tile_size),
  }
}

function tilesAtLevel(meta: ItemTiles, level: number, rect: ImageRect): VisibleTile[] {
  const tileOrigSize = meta.tile_size * 2 ** (meta.max_level - level)
  const { cols, rows } = tileGridSize(meta, level)
  if (cols <= 0 || rows <= 0) return []

  const colMin = Math.max(0, Math.floor(rect.x / tileOrigSize))
  const rowMin = Math.max(0, Math.floor(rect.y / tileOrigSize))
  const colMax = Math.min(cols - 1, Math.floor((rect.x + rect.width) / tileOrigSize))
  const rowMax = Math.min(rows - 1, Math.floor((rect.y + rect.height) / tileOrigSize))
  if (colMax < colMin || rowMax < rowMin) return []

  const tiles: VisibleTile[] = []
  for (let row = rowMin; row <= rowMax; row++) {
    const y = row * tileOrigSize
    for (let col = colMin; col <= colMax; col++) {
      const x = col * tileOrigSize
      tiles.push({
        level,
        col,
        row,
        x,
        y,
        w: Math.min(tileOrigSize, meta.width - x),
        h: Math.min(tileOrigSize, meta.height - y),
      })
    }
  }
  return tiles
}

/**
 * Tiles covering `visibleRect` (original image pixels) at `level`. Capped at
 * `MAX_VISIBLE_TILES`: a viewport that would need more (a very large or
 * oddly-shaped stage) steps one level coarser and retries, down to level 0.
 */
export function visibleTiles(meta: ItemTiles, level: number, visibleRect: ImageRect): VisibleTile[] {
  let current = Math.min(meta.max_level, Math.max(0, level))
  let tiles = tilesAtLevel(meta, current, visibleRect)
  while (tiles.length > MAX_VISIBLE_TILES && current > 0) {
    current -= 1
    tiles = tilesAtLevel(meta, current, visibleRect)
  }
  return tiles
}
