/**
 * Pure helpers for `llm` items' `ranking` / `rating` shapes (CONTRACTS "LLM
 * evaluation items"): finding, creating and removing the at-most-one shape
 * per (class) or (class, target), and parsing the item's document.
 */

import type { LlmDocument, RankingShape, RatingShape, Shape } from '@/api/types'

export function isRanking(shape: Shape): shape is RankingShape {
  return shape.type === 'ranking'
}

export function isRating(shape: Shape): shape is RatingShape {
  return shape.type === 'rating'
}

/** The at-most-one ranking shape of a class. */
export function rankingFor(shapes: readonly Shape[], cls: string): RankingShape | undefined {
  return shapes.filter(isRanking).find((shape) => shape.class === cls)
}

/** The at-most-one rating shape of a (class, target) pair. */
export function ratingFor(
  shapes: readonly Shape[],
  cls: string,
  target: string,
): RatingShape | undefined {
  return shapes.filter(isRating).find((shape) => shape.class === cls && shape.target === target)
}

/** Replace a class's ranking shape (there is at most one) with `shape`. */
export function upsertRanking(shapes: readonly Shape[], shape: RankingShape): Shape[] {
  return [...shapes.filter((s) => !(isRanking(s) && s.class === shape.class)), shape]
}

/** Replace a (class, target) pair's rating shape (there is at most one) with `shape`. */
export function upsertRating(shapes: readonly Shape[], shape: RatingShape): Shape[] {
  return [
    ...shapes.filter((s) => !(isRating(s) && s.class === shape.class && s.target === shape.target)),
    shape,
  ]
}

/** Remove a (class, target) pair's rating shape, if any. */
export function removeRating(shapes: readonly Shape[], cls: string, target: string): Shape[] {
  return shapes.filter((s) => !(isRating(s) && s.class === cls && s.target === target))
}

export function responseTarget(id: string): string {
  return `response:${id}`
}

export function messageTarget(index: number): string {
  return `message:${index}`
}

export const CONVERSATION_TARGET = 'conversation'

/** The order to show/reorder a ranking class by: its existing shape's order,
 * or the responses in document order when none exists yet. */
export function effectiveOrder(
  shapes: readonly Shape[],
  cls: string,
  responseIds: readonly string[],
): string[] {
  return rankingFor(shapes, cls)?.order ?? [...responseIds]
}

/** Swap the entry at `index` with its neighbour one slot up (-1) or down
 * (+1); returns the same order (a new array) when already at that end. */
export function moveInOrder(order: readonly string[], index: number, direction: -1 | 1): string[] {
  const to = index + direction
  const next = [...order]
  if (to < 0 || to >= order.length) return next
  ;[next[index], next[to]] = [next[to], next[index]]
  return next
}

/**
 * Parse an `llm` item's document from the raw text its signed `media_url`
 * serves. `null` on invalid JSON, a non-object, or an object whose
 * `messages` and `responses` are both empty (CONTRACTS forbids both empty).
 */
export function parseLlmDocument(raw: string): LlmDocument | null {
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return null
  const obj = parsed as Record<string, unknown>
  const messages = Array.isArray(obj.messages) ? (obj.messages as LlmDocument['messages']) : []
  const responses = Array.isArray(obj.responses) ? (obj.responses as LlmDocument['responses']) : []
  if (messages.length === 0 && responses.length === 0) return null
  const meta =
    typeof obj.meta === 'object' && obj.meta !== null
      ? (obj.meta as Record<string, unknown>)
      : undefined
  return { messages, responses, meta }
}
