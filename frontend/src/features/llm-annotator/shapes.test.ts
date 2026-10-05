import { describe, expect, it } from 'vitest'

import type { RankingShape, RatingShape, Shape } from '@/api/types'

import {
  CONVERSATION_TARGET,
  effectiveOrder,
  messageTarget,
  moveInOrder,
  parseLlmDocument,
  rankingFor,
  ratingFor,
  removeRating,
  responseTarget,
  upsertRanking,
  upsertRating,
} from './shapes'

function ranking(cls: string, order: string[]): RankingShape {
  return { id: `${cls}-ranking`, type: 'ranking', class: cls, attributes: {}, confidence: null, order }
}

function rating(cls: string, target: string, value: number): RatingShape {
  return { id: `${cls}-${target}`, type: 'rating', class: cls, attributes: {}, confidence: null, target, value }
}

describe('moveInOrder', () => {
  it('swaps with the previous entry', () => {
    expect(moveInOrder(['a', 'b', 'c'], 1, -1)).toEqual(['b', 'a', 'c'])
  })

  it('swaps with the next entry', () => {
    expect(moveInOrder(['a', 'b', 'c'], 1, 1)).toEqual(['a', 'c', 'b'])
  })

  it('is a no-op (but a new array) past either end', () => {
    const order = ['a', 'b', 'c']
    expect(moveInOrder(order, 0, -1)).toEqual(order)
    expect(moveInOrder(order, 2, 1)).toEqual(order)
  })
})

describe('ranking upsert', () => {
  it('finds the ranking of a class', () => {
    const shapes: Shape[] = [ranking('preference', ['a', 'b'])]
    expect(rankingFor(shapes, 'preference')?.order).toEqual(['a', 'b'])
    expect(rankingFor(shapes, 'other')).toBeUndefined()
  })

  it('replaces the one ranking a class may have', () => {
    const shapes: Shape[] = [ranking('preference', ['a', 'b']), ranking('quality', ['x'])]
    const next = upsertRanking(shapes, ranking('preference', ['b', 'a']))
    expect(next).toHaveLength(2)
    expect(rankingFor(next, 'preference')?.order).toEqual(['b', 'a'])
    expect(rankingFor(next, 'quality')?.order).toEqual(['x'])
  })

  it('falls back to document order with no ranking yet', () => {
    expect(effectiveOrder([], 'preference', ['a', 'b', 'c'])).toEqual(['a', 'b', 'c'])
  })

  it('uses the existing ranking order once one exists', () => {
    const shapes: Shape[] = [ranking('preference', ['c', 'a', 'b'])]
    expect(effectiveOrder(shapes, 'preference', ['a', 'b', 'c'])).toEqual(['c', 'a', 'b'])
  })
})

describe('rating upsert / remove', () => {
  it('replaces the one rating a (class, target) pair may have', () => {
    const shapes: Shape[] = [rating('helpfulness', responseTarget('a'), 3)]
    const next = upsertRating(shapes, rating('helpfulness', responseTarget('a'), 5))
    expect(next).toHaveLength(1)
    expect(ratingFor(next, 'helpfulness', responseTarget('a'))?.value).toBe(5)
  })

  it('keeps ratings of other targets or classes untouched', () => {
    const shapes: Shape[] = [
      rating('helpfulness', responseTarget('a'), 3),
      rating('helpfulness', messageTarget(0), 2),
      rating('safety', responseTarget('a'), 1),
    ]
    const next = upsertRating(shapes, rating('helpfulness', responseTarget('a'), 5))
    expect(next).toHaveLength(3)
    expect(ratingFor(next, 'helpfulness', messageTarget(0))?.value).toBe(2)
    expect(ratingFor(next, 'safety', responseTarget('a'))?.value).toBe(1)
  })

  it('removes a rating by (class, target)', () => {
    const shapes: Shape[] = [
      rating('helpfulness', CONVERSATION_TARGET, 4),
      rating('safety', CONVERSATION_TARGET, 2),
    ]
    const next = removeRating(shapes, 'helpfulness', CONVERSATION_TARGET)
    expect(next).toEqual([rating('safety', CONVERSATION_TARGET, 2)])
  })
})

describe('parseLlmDocument', () => {
  it('parses messages and responses', () => {
    const doc = parseLlmDocument(
      JSON.stringify({ messages: [{ role: 'user', content: 'hi' }], responses: [] }),
    )
    expect(doc).toEqual({ messages: [{ role: 'user', content: 'hi' }], responses: [], meta: undefined })
  })

  it('is null on invalid JSON', () => {
    expect(parseLlmDocument('not json')).toBeNull()
  })

  it('is null when both messages and responses are empty or absent', () => {
    expect(parseLlmDocument('{}')).toBeNull()
    expect(parseLlmDocument(JSON.stringify({ messages: [], responses: [] }))).toBeNull()
  })

  it('is null for a JSON array or primitive', () => {
    expect(parseLlmDocument('[1,2,3]')).toBeNull()
    expect(parseLlmDocument('42')).toBeNull()
  })
})
