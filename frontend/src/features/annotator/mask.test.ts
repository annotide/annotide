import { describe, expect, it } from 'vitest'
import {
  canBrush,
  decodeRle,
  emptyBitmap,
  encodeRle,
  hexToRgb,
  isEmptyRle,
  maskPixels,
  MAX_MASK_PIXELS,
  paintDisc,
  paintStroke,
  rleArea,
  rleBounds,
} from './mask'

describe('RLE (column-major COCO counts)', () => {
  it('round-trips a bitmap', () => {
    const bitmap = emptyBitmap(4, 3)
    // pixel (x=1, y=2) and the whole of column 3
    bitmap.bits[1 * 3 + 2] = 1
    bitmap.bits.fill(1, 9, 12)
    const rle = encodeRle(bitmap)
    expect(rle).toEqual({ size: [3, 4], counts: [5, 1, 3, 3] })
    expect(rle.counts.reduce((a, b) => a + b, 0)).toBe(12)
    expect(decodeRle(rle)?.bits).toEqual(bitmap.bits)
  })

  it('starts with a background run even when the first pixel is set', () => {
    const bitmap = emptyBitmap(2, 2)
    bitmap.bits[0] = 1
    expect(encodeRle(bitmap).counts).toEqual([0, 1, 3])
  })

  it('rejects counts that do not cover the mask', () => {
    expect(decodeRle({ size: [2, 2], counts: [1, 1] })).toBeNull()
    expect(decodeRle({ size: [2, 2], counts: [3, 3] })).toBeNull()
  })

  it('reports area, emptiness and bounds', () => {
    const rle = { size: [3, 4] as [number, number], counts: [5, 1, 3, 3] }
    expect(rleArea(rle)).toBe(4)
    expect(isEmptyRle(rle)).toBe(false)
    expect(isEmptyRle({ size: [3, 4], counts: [12] })).toBe(true)
    expect(rleBounds(rle)).toEqual([1, 0, 4, 3])
    expect(rleBounds({ size: [3, 4], counts: [4, 1, 7] })).toEqual([1, 1, 2, 2])
    expect(rleBounds({ size: [3, 4], counts: [12] })).toBeNull()
  })

  it('treats a run across columns as covering the full height', () => {
    // index 2 (x0,y2) through 3 (x1,y0)
    expect(rleBounds({ size: [3, 4], counts: [2, 2, 8] })).toEqual([0, 0, 2, 3])
  })
})

describe('painting', () => {
  it('paints a disc of pixel centres within the radius', () => {
    const bitmap = emptyBitmap(10, 10)
    paintDisc(bitmap, [5, 5], 1.5, 1)
    const rle = encodeRle(bitmap)
    expect(rleBounds(rle)).toEqual([4, 4, 6, 6])
    expect(rleArea(rle)).toBe(4)
  })

  it('clips at the image edge', () => {
    const bitmap = emptyBitmap(10, 10)
    paintDisc(bitmap, [0, 0], 3, 1)
    expect(rleBounds(encodeRle(bitmap))).toEqual([0, 0, 3, 3])
  })

  it('fills a fast stroke without gaps and erases with value 0', () => {
    const bitmap = emptyBitmap(100, 20)
    paintStroke(bitmap, [[5, 10], [95, 10]], 2, 1)
    for (let x = 5; x < 95; x += 1) expect(bitmap.bits[x * 20 + 10]).toBe(1)
    paintStroke(bitmap, [[50, 10]], 5, 0)
    expect(bitmap.bits[50 * 20 + 10]).toBe(0)
    expect(bitmap.bits[20 * 20 + 10]).toBe(1)
  })

  it('limits brushing to images that fit the pixel budget', () => {
    expect(canBrush(4000, 3000)).toBe(true)
    expect(canBrush(MAX_MASK_PIXELS + 1, 1)).toBe(false)
    expect(canBrush(0, 10)).toBe(false)
  })
})

describe('display', () => {
  it('writes RGBA row-major inside the bounds', () => {
    const rle = { size: [3, 4] as [number, number], counts: [5, 1, 3, 3] }
    const bounds = rleBounds(rle)!
    const pixels = maskPixels(rle, bounds, [1, 2, 3], 200)
    const w = bounds[2] - bounds[0]
    const at = (x: number, y: number) => Array.from(pixels.slice((y * w + x) * 4, (y * w + x) * 4 + 4))
    expect(at(0, 2)).toEqual([1, 2, 3, 200]) // image (1, 2)
    expect(at(2, 0)).toEqual([1, 2, 3, 200]) // image (3, 0)
    expect(at(0, 0)).toEqual([0, 0, 0, 0])
  })

  it('parses hex colours', () => {
    expect(hexToRgb('#ff8000')).toEqual([255, 128, 0])
    expect(hexToRgb('red')).toEqual([128, 128, 128])
  })
})
