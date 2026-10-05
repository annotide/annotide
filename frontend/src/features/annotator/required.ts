import type { LabelSchemaDefinition } from '@/api/types'
import type { AnnotationResult } from './types'

/** One required attribute the result lacks; `shapeId` is null for the
 * item-level classification. */
export interface MissingAttribute {
  shapeId: string | null
  attribute: string
}

/**
 * The required attributes a result is missing, in the order the server's
 * QA-6 validation reports them: classification first, then each shape's.
 * Mirrors `validate_against_schema` (absent key = missing; the attribute
 * editor removes cleared values rather than storing `""`), so submit can stop
 * before a round trip that is bound to fail. Shapes of a class the schema
 * lacks are left to the server, which reports them its own way.
 */
export function missingRequiredAttributes(
  result: AnnotationResult,
  definition: LabelSchemaDefinition,
): MissingAttribute[] {
  const missing: MissingAttribute[] = []
  for (const field of definition.classification) {
    if (field.required && !(field.name in result.classification)) {
      missing.push({ shapeId: null, attribute: field.name })
    }
  }
  const classes = new Map(definition.classes.map((cls) => [cls.name, cls]))
  for (const shape of result.shapes) {
    for (const attribute of classes.get(shape.class)?.attributes ?? []) {
      if (attribute.required && !(attribute.name in shape.attributes)) {
        missing.push({ shapeId: shape.id, attribute: attribute.name })
      }
    }
  }
  return missing
}
