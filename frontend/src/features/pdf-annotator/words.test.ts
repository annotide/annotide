import { describe, expect, it } from 'vitest'
import { multiply, spanGeometry, textInBox, wordAt, wordBoxes } from './words'
import type { Word } from './words'

// A scale-1 viewport of a 600 × 800 pt page: flip y about the page height.
const VIEWPORT = [1, 0, 0, -1, 0, 800]

describe('wordBoxes', () => {
  it('places words by character share, in top-left y-down points', () => {
    // "Total 42" at x=100, baseline y=700 (PDF space, y up), 10 pt font, 80 pt wide.
    const words = wordBoxes(
      [{ str: 'Total 42', transform: [10, 0, 0, 10, 100, 700], width: 80 }],
      VIEWPORT,
    )
    expect(words.map((w) => w.text)).toEqual(['Total', '42'])
    // 8 characters over 80 pt: "Total" spans 0–50, "42" spans 60–80.
    expect(words[0].bbox).toEqual([100, 90, 150, 100])
    expect(words[1].bbox).toEqual([160, 90, 180, 100])
  })

  it('skips empty runs and counts code points, not UTF-16 units', () => {
    const words = wordBoxes(
      [
        { str: '', transform: [10, 0, 0, 10, 0, 0], width: 0 },
        { str: '😀 ab', transform: [10, 0, 0, 10, 0, 700], width: 40 },
      ],
      VIEWPORT,
    )
    expect(words.map((w) => w.text)).toEqual(['😀', 'ab'])
    expect(words[1].bbox[0]).toBeCloseTo(20)
  })

  it('handles a page rotated by 90 degrees', () => {
    // /Rotate 90 on a 600 × 800 page: viewport is 800 × 600.
    const rotated = [0, 1, 1, 0, 0, 0]
    const [word] = wordBoxes(
      [{ str: 'ab', transform: [10, 0, 0, 10, 100, 200], width: 20 }],
      rotated,
    )
    // Baseline runs down the screen now; the box is tall and narrow.
    expect(word.bbox[3] - word.bbox[1]).toBeCloseTo(20)
    expect(word.bbox[2] - word.bbox[0]).toBeCloseTo(10)
  })

  it('multiplies like pdf.js Util.transform', () => {
    expect(multiply([2, 0, 0, 2, 5, 5], [1, 0, 0, 1, 3, 4])).toEqual([2, 0, 0, 2, 11, 13])
  })
})

describe('textInBox', () => {
  const words = wordBoxes(
    [
      { str: 'Invoice total', transform: [10, 0, 0, 10, 100, 700], width: 130 },
      { str: '42,00 €', transform: [10, 0, 0, 10, 100, 685], width: 70 },
      { str: 'Page 1', transform: [8, 0, 0, 8, 500, 40], width: 48 },
    ],
    VIEWPORT,
  )

  it('joins the words inside in reading order', () => {
    expect(textInBox(words, [90, 80, 260, 120])).toBe('Invoice total 42,00 €')
  })

  it('takes a word only when its centre is inside', () => {
    expect(textInBox(words, [90, 80, 160, 102])).toBe('Invoice')
  })

  it('is undefined over an empty area', () => {
    expect(textInBox(words, [0, 0, 50, 50])).toBeUndefined()
  })
})

describe('span selection', () => {
  // Two lines: "Alice Smith" then "met Bob", 10 pt words.
  const line = (str: string, y: number): Word[] =>
    wordBoxes([{ str, transform: [10, 0, 0, 10, 100, y], width: str.length * 10 }], VIEWPORT)
  const words = [...line('Alice Smith', 700), ...line('met Bob', 686)]

  it('finds the word under a point, or the nearest one in a gap', () => {
    expect(wordAt(words, [110, 95])).toBe(0)
    expect(wordAt(words, [5, 5])).toBe(-1)
    expect(wordAt(words, [5, 5], true)).toBeGreaterThanOrEqual(0)
    expect(words[wordAt(words, [100, 109], true)].text).toBe('met')
  })

  it('groups a run into one union box per line', () => {
    const geometry = spanGeometry(words, 1, 2)!
    expect(geometry.text).toBe('Smith met')
    expect(geometry.boxes).toHaveLength(2)
    expect(geometry.boxes[0]).toEqual(words[1].bbox)
    expect(geometry.boxes[1]).toEqual(words[2].bbox)
    const all = spanGeometry(words, 0, 1)!
    expect(all.boxes).toEqual([[100, 90, 210, 100]])
  })

  it('accepts either direction and rejects an empty run', () => {
    expect(spanGeometry(words, 3, 2)).toEqual(spanGeometry(words, 2, 3))
    expect(spanGeometry([], 0, 0)).toBeNull()
  })

  it('clamps a word past the page edge to the page', () => {
    const edge: Word[] = [{ text: 'Cut', bbox: [-4, -2, 20, 10] }]
    expect(spanGeometry(edge, 0, 0)!.boxes).toEqual([[0, 0, 20, 10]])
    const offPage: Word[] = [{ text: 'Gone', bbox: [-20, 5, -2, 10] }]
    expect(spanGeometry(offPage, 0, 0)).toBeNull()
  })
})
