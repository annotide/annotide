import { describe, expect, it } from 'vitest'
import type { LabelSchemaDefinition } from '@/api/types'
import { missingRequiredAttributes } from './required'
import type { AnnotationResult, Shape } from './types'

const definition = {
  version: 1,
  classes: [
    {
      name: 'car',
      attributes: [
        { name: 'plate', type: 'text', required: true },
        { name: 'color', type: 'text', required: false },
      ],
    },
    { name: 'person', attributes: [] },
  ],
  classification: [
    { name: 'weather', type: 'select', required: true, options: ['sun', 'rain'] },
    { name: 'note', type: 'text', required: false },
  ],
} as unknown as LabelSchemaDefinition

function box(id: string, cls: string, attributes: Shape['attributes'] = {}): Shape {
  return { id, type: 'bbox', class: cls, attributes, confidence: null, bbox: [0, 0, 1, 1] }
}

function result(shapes: Shape[], classification: AnnotationResult['classification']) {
  return { schema_version: 1, media_type: 'image', classification, shapes } as AnnotationResult
}

describe('missingRequiredAttributes', () => {
  it('lists the classification first, then each shape', () => {
    const missing = missingRequiredAttributes(
      result([box('a', 'car'), box('b', 'person'), box('c', 'car', { plate: 'X' })], {}),
      definition,
    )
    expect(missing).toEqual([
      { shapeId: null, attribute: 'weather' },
      { shapeId: 'a', attribute: 'plate' },
    ])
  })

  it('is empty once every required value is set', () => {
    const complete = result([box('a', 'car', { plate: 'ABC-123' })], { weather: 'sun' })
    expect(missingRequiredAttributes(complete, definition)).toEqual([])
  })

  it('leaves shapes of an unknown class to the server', () => {
    const unknown = result([box('a', 'truck')], { weather: 'rain' })
    expect(missingRequiredAttributes(unknown, definition)).toEqual([])
  })
})
