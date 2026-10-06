import { describe, expect, it } from 'vitest'

import type { RelationShape, TextSpanShape } from '@/api/types'

import { codePointToUtf16, isSpan, removeShape, segmentText, trimRange, utf16ToCodePoint } from './offsets'

function span(id: string, start: number, end: number, cls = 'PER'): TextSpanShape {
  return {
    id,
    type: 'span',
    class: cls,
    attributes: {},
    confidence: null,
    start,
    end,
  }
}

describe('code point offsets', () => {
  // "a😀b": 😀 is one code point but two UTF-16 units.
  const text = 'a\u{1F600}b'

  it('converts UTF-16 offsets to code points past a surrogate pair', () => {
    expect(utf16ToCodePoint(text, 0)).toBe(0)
    expect(utf16ToCodePoint(text, 1)).toBe(1)
    expect(utf16ToCodePoint(text, 3)).toBe(2)
    expect(utf16ToCodePoint(text, 4)).toBe(3)
  })

  it('converts code points back to UTF-16 offsets', () => {
    expect(codePointToUtf16(text, 2)).toBe(3)
    expect(codePointToUtf16(text, 3)).toBe(4)
  })
})

describe('trimRange', () => {
  const chars = Array.from('  Alice  ')

  it('drops surrounding whitespace and orders the ends', () => {
    expect(trimRange(chars, 9, 0)).toEqual([2, 7])
  })

  it('is null for whitespace only', () => {
    expect(trimRange(chars, 0, 2)).toBeNull()
  })
})

describe('segmentText', () => {
  it('cuts at every boundary and lists covering spans outermost first', () => {
    const outer = span('outer', 0, 10, 'ORG')
    const inner = span('inner', 2, 5)
    const segments = segmentText(12, [inner, outer])
    expect(segments.map((s) => [s.start, s.end])).toEqual([
      [0, 2],
      [2, 5],
      [5, 10],
      [10, 12],
    ])
    expect(segments[1].spans.map((s) => s.id)).toEqual(['outer', 'inner'])
    expect(segments[3].spans).toEqual([])
  })

  it('handles partially overlapping spans', () => {
    const segments = segmentText(8, [span('a', 0, 5), span('b', 3, 8)])
    expect(segments.map((s) => s.spans.map((x) => x.id))).toEqual([['a'], ['a', 'b'], ['b']])
  })
})

describe('removeShape', () => {
  it('removes a span together with its relations', () => {
    const rel: RelationShape = {
      id: 'r',
      type: 'relation',
      class: 'works_for',
      attributes: {},
      confidence: null,
      from: 'a',
      to: 'b',
    }
    const shapes = [span('a', 0, 1), span('b', 2, 3), rel]
    expect(removeShape(shapes, 'a').map((s) => s.id)).toEqual(['b'])
    expect(removeShape(shapes, 'r').map((s) => s.id)).toEqual(['a', 'b'])
  })
})

describe('isSpan', () => {
  it('takes text spans as the API returns them, with null pdf fields', () => {
    const fromApi = { ...span('a', 0, 5), boxes: null, page: null }
    expect(isSpan(fromApi)).toBe(true)
  })

  it('leaves pdf spans out', () => {
    expect(
      isSpan({ ...span('p', 0, 1), page: 1, boxes: [[1, 1, 5, 5]], start: null, end: null }),
    ).toBe(false)
  })
})
