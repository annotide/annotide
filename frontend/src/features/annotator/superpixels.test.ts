import { describe, expect, it } from 'vitest'

import { emptyBitmap } from './mask'
import {
  boundaryPixels,
  fillSuperpixels,
  labelAt,
  labelsAlong,
  segment,
  slic,
  superpixelPixels,
  workingSize,
} from './superpixels'

/** RGBA image: left half red, right half blue. */
function halves(width: number, height: number): Uint8ClampedArray {
  const rgba = new Uint8ClampedArray(width * height * 4)
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const o = (y * width + x) * 4
      rgba.set(x < width / 2 ? [220, 30, 30, 255] : [30, 30, 220, 255], o)
    }
  }
  return rgba
}

/** Every label's pixels form one 4-connected region. */
function isConnected(labels: Int32Array, width: number, height: number, count: number): boolean {
  const seen = new Uint8Array(labels.length)
  const regions = new Array<number>(count).fill(0)
  for (let start = 0; start < labels.length; start += 1) {
    if (seen[start]) continue
    const label = labels[start]
    regions[label] += 1
    const stack = [start]
    seen[start] = 1
    while (stack.length > 0) {
      const i = stack.pop()!
      const x = i % width
      for (const j of [x > 0 ? i - 1 : -1, x < width - 1 ? i + 1 : -1, i - width, i + width]) {
        if (j < 0 || j >= width * height || seen[j] || labels[j] !== label) continue
        seen[j] = 1
        stack.push(j)
      }
    }
  }
  return regions.every((r) => r === 1)
}

describe('workingSize', () => {
  it('keeps small images and downscales large ones to the pixel budget', () => {
    expect(workingSize(800, 600)).toEqual({ width: 800, height: 600, scale: 1 })
    const big = workingSize(8000, 6000)
    expect(big.scale).toBeCloseTo(Math.sqrt(1_000_000 / 48_000_000))
    expect(big.width * big.height).toBeLessThanOrEqual(1_000_000 + big.width + big.height)
  })
})

describe('slic', () => {
  it('gives roughly the requested number of connected superpixels', () => {
    const { labels, count } = slic(halves(120, 80), 120, 80, 60)
    expect(count).toBeGreaterThan(30)
    expect(count).toBeLessThan(120)
    expect(Math.min(...labels)).toBe(0)
    expect(Math.max(...labels)).toBe(count - 1)
    expect(isConnected(labels, 120, 80, count)).toBe(true)
  })

  it('never lets a superpixel straddle a strong colour edge', () => {
    const { labels } = slic(halves(120, 80), 120, 80, 60)
    const left = new Set<number>()
    const right = new Set<number>()
    for (let i = 0; i < labels.length; i += 1) {
      const side = i % 120 < 60 ? left : right
      side.add(labels[i])
    }
    expect([...left].filter((label) => right.has(label))).toEqual([])
  })
})

describe('segment and fill', () => {
  it('maps image points through the working scale', () => {
    const seg = segment(halves(60, 40), 60, 40, 0.5)
    expect(labelAt(seg, [0, 0])).toBe(seg.labels[0])
    expect(labelAt(seg, [119, 79])).toBe(seg.labels[seg.labels.length - 1])
    expect(labelAt(seg, [120, 0])).toBe(-1)
  })

  it('collects every superpixel a straight path crosses, however long the step', () => {
    const seg = segment(halves(60, 40), 60, 40, 0.5)
    const along = labelsAlong(seg, [0, 40], [119, 40])
    const expected = new Set<number>()
    for (let x = 0; x < 60; x += 1) expected.add(seg.labels[20 * 60 + x])
    expect(new Set(along)).toEqual(expected)
    expect(labelsAlong(seg, [10, 10], [10, 10])).toEqual([labelAt(seg, [10, 10])])
  })

  it('fills exactly the chosen superpixels into a full-resolution mask', () => {
    // Working 60×40 for a 120×80 image.
    const seg = segment(halves(60, 40), 60, 40, 0.5)
    const label = labelAt(seg, [10, 10])
    const bitmap = emptyBitmap(120, 80)
    fillSuperpixels(bitmap, seg, [label], 1)
    let set = 0
    for (let x = 0; x < 120; x += 1) {
      for (let y = 0; y < 80; y += 1) {
        const inside = labelAt(seg, [x, y]) === label
        expect(bitmap.bits[x * 80 + y]).toBe(inside ? 1 : 0)
        if (inside) set += 1
      }
    }
    expect(set).toBeGreaterThan(0)

    fillSuperpixels(bitmap, seg, [label], 0)
    expect(bitmap.bits.every((b) => b === 0)).toBe(true)
  })

  it('draws boundaries between superpixels and previews one', () => {
    const seg = segment(halves(60, 40), 60, 40, 1)
    const edges = boundaryPixels(seg, [255, 255, 255, 200])
    // The colour edge at x = 29 | 30 is a superpixel boundary on every row.
    for (let y = 0; y < 40; y += 1) expect(edges[(y * 60 + 29) * 4 + 3]).toBe(200)

    const preview = superpixelPixels(seg, 0, [1, 2, 3, 4])
    expect(preview.x).toBe(seg.bounds[0])
    expect(preview.data.length).toBe(preview.width * preview.height * 4)
    expect(preview.data.some((v) => v === 4)).toBe(true)
  })
})
