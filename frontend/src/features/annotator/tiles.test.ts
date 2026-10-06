import { describe, expect, it } from 'vitest'

import {
  levelForScale,
  levelSize,
  MAX_VISIBLE_TILES,
  tileGridSize,
  tileKey,
  visibleTiles,
} from './tiles'
import type { ItemTiles } from './types'

/** 10000x8000 px, 256 px tiles, no overlap — same shape as CONTRACTS.md. */
const META: ItemTiles = {
  format: 'dzi',
  path: 'cache/tiles/item-1/',
  tile_size: 256,
  overlap: 0,
  suffix: 'jpeg',
  max_level: 14, // ceil(log2(10000)) = 14
  width: 10000,
  height: 8000,
}

describe('levelForScale', () => {
  it('uses max_level at 1:1 or more', () => {
    expect(levelForScale(1, META.max_level)).toBe(14)
    expect(levelForScale(4, META.max_level)).toBe(14)
  })

  it('picks a coarser level zoomed out, never finer than needed', () => {
    // scale 0.5 -> log2(1/0.5)=1 -> level = max-1
    expect(levelForScale(0.5, META.max_level)).toBe(13)
    // scale 0.25 -> level = max-2
    expect(levelForScale(0.25, META.max_level)).toBe(12)
    // scale 0.1 -> log2(10)=3.32, floor=3 -> level = max-3
    expect(levelForScale(0.1, META.max_level)).toBe(11)
  })

  it('clamps to [0, maxLevel]', () => {
    expect(levelForScale(0.00001, META.max_level)).toBe(0)
    expect(levelForScale(1000, META.max_level)).toBe(META.max_level)
  })

  it('treats a non-positive scale as level 0', () => {
    expect(levelForScale(0, META.max_level)).toBe(0)
    expect(levelForScale(-1, META.max_level)).toBe(0)
  })
})

describe('levelSize', () => {
  it('level 0 is always 1x1', () => {
    expect(levelSize(META, 0)).toEqual({ width: 1, height: 1 })
  })

  it('the top level is the full image size', () => {
    expect(levelSize(META, META.max_level)).toEqual({ width: 10000, height: 8000 })
  })

  it('matches ceil(dimension / 2^(max_level - level))', () => {
    const level = META.max_level - 1
    expect(levelSize(META, level)).toEqual({
      width: Math.ceil(10000 / 2),
      height: Math.ceil(8000 / 2),
    })
  })
})

describe('tileGridSize', () => {
  it('covers the level with tile_size tiles, rounding up for the edge', () => {
    const { cols, rows } = tileGridSize(META, META.max_level)
    expect(cols).toBe(Math.ceil(10000 / 256))
    expect(rows).toBe(Math.ceil(8000 / 256))
  })
})

describe('visibleTiles', () => {
  it('returns the single tile at level 0', () => {
    const tiles = visibleTiles(META, 0, { x: 0, y: 0, width: 10000, height: 8000 })
    expect(tiles).toEqual([{ level: 0, col: 0, row: 0, x: 0, y: 0, w: 10000, h: 8000 }])
  })

  it('covers a viewport rect with full-size tiles at the top level', () => {
    const tiles = visibleTiles(META, META.max_level, { x: 0, y: 0, width: 300, height: 300 })
    // 300px viewport at 256px tiles touches columns/rows 0 and 1.
    const keys = tiles.map(tileKey).sort()
    expect(keys).toEqual(['14/0_0', '14/0_1', '14/1_0', '14/1_1'])
    for (const tile of tiles) {
      if (tile.col < 39 && tile.row < 31) {
        expect(tile.w).toBe(256)
        expect(tile.h).toBe(256)
      }
    }
  })

  it('shrinks edge tiles to the image boundary', () => {
    const level = META.max_level
    const { cols, rows } = tileGridSize(META, level)
    const lastCol = cols - 1
    const lastRow = rows - 1
    const tiles = visibleTiles(META, level, {
      x: lastCol * 256,
      y: lastRow * 256,
      width: 256,
      height: 256,
    })
    const corner = tiles.find((t) => t.col === lastCol && t.row === lastRow)
    expect(corner).toBeDefined()
    expect(corner?.w).toBe(10000 - lastCol * 256)
    expect(corner?.h).toBe(8000 - lastRow * 256)
    expect(corner?.w).toBeLessThan(256)
    expect(corner?.h).toBeLessThan(256)
  })

  it('clamps a rect that runs past the image edge to the grid', () => {
    const tiles = visibleTiles(META, 0, { x: -500, y: -500, width: 20000, height: 20000 })
    expect(tiles).toEqual([{ level: 0, col: 0, row: 0, x: 0, y: 0, w: 10000, h: 8000 }])
  })

  it('returns nothing for a rect entirely outside the image', () => {
    const tiles = visibleTiles(META, META.max_level, {
      x: 50000,
      y: 50000,
      width: 100,
      height: 100,
    })
    expect(tiles).toEqual([])
  })

  it('caps at MAX_VISIBLE_TILES by falling back one level coarser', () => {
    // The whole image at the top level needs far more than 256 tiles.
    const tiles = visibleTiles(META, META.max_level, { x: 0, y: 0, width: 10000, height: 8000 })
    expect(tiles.length).toBeLessThanOrEqual(MAX_VISIBLE_TILES)
    // Every returned tile is at the same (coarser) level.
    const levels = new Set(tiles.map((t) => t.level))
    expect(levels.size).toBe(1)
    expect(tiles[0]?.level).toBeLessThan(META.max_level)
  })

  it('never returns more than MAX_VISIBLE_TILES even at level 0', () => {
    // A pathological grid tiny enough that even level 0 has one tile: fine.
    const tiles = visibleTiles(META, 0, { x: 0, y: 0, width: 10000, height: 8000 })
    expect(tiles.length).toBeLessThanOrEqual(MAX_VISIBLE_TILES)
  })
})
