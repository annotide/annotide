/**
 * The annotator's types. The wire types (label schema, annotation result,
 * shapes — docs/CONTRACTS.md) are `src/api/types.ts`'s, re-exported so
 * call sites inside this feature keep importing from `./types`; only the
 * editor's own vocabulary is declared here.
 */

export type {
  AnnotationResult,
  AttributeType,
  AttributeValue,
  BBox,
  BBoxShape,
  ItemTiles,
  Keypoint,
  KeypointsShape,
  KeypointVisibility,
  LabelAttribute,
  LabelClass,
  MaskShape,
  MediaType,
  Point2D,
  PointShape,
  PolygonShape,
  PolylineShape,
  RBoxShape,
  Shape,
  Skeleton,
  ShapeAttributes,
  ShapeTool,
  TileKey,
} from '@/api/types'

import type { BBox, Point2D } from '@/api/types'

/** `smart` is the model-assisted polygon (ML-7): a click or a dragged box is
 * sent to a segment model and the polygon it answers is added as a shape. */
export type Tool =
  | 'select'
  /** The hand: drag anywhere to move the view; never edits. */
  | 'pan'
  | 'bbox'
  | 'rbox'
  | 'polygon'
  | 'polyline'
  | 'point'
  | 'smart'
  /** The ruler (TOOL-8): measures, never creates a shape; works read-only too. */
  | 'measure'
  /** Pixel mask brush and eraser: paint into the selected mask, or start a
   * new one in the active class; the eraser only edits the selected mask. */
  | 'brush'
  | 'eraser'
  /** Superpixels: click or drag to add whole superpixels to the selected
   * mask (or a new one in the active class); Alt removes them. */
  | 'superpixel'
  /** Skeleton keypoints: one click per named point of the class's skeleton. */
  | 'keypoints'

/** What the smart tool asks the model: one click, or one box, in image pixels. */
export type SmartPrompt = { kind: 'point'; point: Point2D } | { kind: 'box'; bbox: BBox }
