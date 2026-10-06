import { describe, expect, it } from 'vitest'
import { copyAnnotations } from './copy'
import type { AnnotationResult, LabelClass, Shape } from './types'

const classes = [{ name: 'car' }, { name: 'person' }] as LabelClass[]

function box(id: string, cls: string): Shape {
  return {
    id,
    type: 'bbox',
    class: cls,
    attributes: { occluded: false },
    confidence: null,
    bbox: [0, 0, 1, 1],
  }
}

function result(
  shapes: Shape[],
  classification: AnnotationResult['classification'] = {},
): AnnotationResult {
  return { schema_version: 1, media_type: 'image', classification, shapes }
}

describe('copyAnnotations', () => {
  it('appends copies with fresh ids and keeps what is already there', () => {
    let n = 0
    const outcome = copyAnnotations(
      result([box('mine', 'person')]),
      result([box('a', 'car'), box('b', 'person')]),
      classes,
      () => `new-${++n}`,
    )
    expect(outcome.result.shapes.map((s) => s.id)).toEqual(['mine', 'new-1', 'new-2'])
    expect(outcome.copied).toBe(2)
    expect(outcome.skipped).toBe(0)
  })

  it('does not share nested objects with the source', () => {
    const source = result([box('a', 'car')])
    const outcome = copyAnnotations(result([]), source, classes, () => 'x')
    outcome.result.shapes[0].attributes.occluded = true
    expect(source.shapes[0].attributes.occluded).toBe(false)
  })

  it('skips classes the schema no longer has', () => {
    const outcome = copyAnnotations(result([]), result([box('a', 'truck')]), classes)
    expect(outcome.result.shapes).toEqual([])
    expect(outcome.skipped).toBe(1)
  })

  it('fills classification only where this item has no value', () => {
    const outcome = copyAnnotations(
      result([], { weather: 'clear' }),
      result([], { weather: 'rain', time: 'night' }),
      classes,
    )
    expect(outcome.result.classification).toEqual({ weather: 'clear', time: 'night' })
  })
})
