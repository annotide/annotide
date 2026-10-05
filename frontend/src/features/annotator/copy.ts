import type { AnnotationResult, LabelClass, Shape } from './types'
import { newShapeId } from '@/lib/ids'

export interface CopyOutcome {
  result: AnnotationResult
  copied: number
  /** Shapes left behind because their class is not in the current schema. */
  skipped: number
}


/**
 * Copy the previous item's shapes onto this one (TOOL-7): appended with new
 * ids, so copying twice gives two sets rather than silently merging. Item
 * classification values are copied only where this item has none yet.
 * Shapes of a class the current schema no longer has are skipped.
 */
export function copyAnnotations(
  target: AnnotationResult,
  source: AnnotationResult,
  classes: LabelClass[],
  makeId: () => string = newShapeId,
): CopyOutcome {
  const known = new Set(classes.map((cls) => cls.name))
  const kept = source.shapes.filter((shape) => known.has(shape.class))
  const shapes: Shape[] = kept.map((shape) => ({
    ...structuredClone(shape),
    id: makeId(),
  }))
  return {
    result: {
      ...target,
      classification: { ...source.classification, ...target.classification },
      shapes: [...target.shapes, ...shapes],
    },
    copied: shapes.length,
    skipped: source.shapes.length - kept.length,
  }
}
