/**
 * Public surface of the image annotator.
 *
 * `AnnotationResult`, `Shape` and `LabelClass` are declared locally in
 * `./types` rather than imported from `@/api/types`, so this feature can be
 * built and tested on its own. They are structurally identical to the wire
 * contract in docs/CONTRACTS.md; a later pass can collapse the duplication by
 * re-exporting the API types once both sides are stable.
 */

export { ImageAnnotator } from './ImageAnnotator'
export type { ImageAnnotatorProps, SelectRequest } from './ImageAnnotator'
export { Toolbar } from './Toolbar'
export type { ToolbarProps } from './Toolbar'
export { TiledImage } from './TiledImage'
export type { TiledImageProps } from './TiledImage'
export { levelForScale, levelSize, tileKey, visibleTiles } from './tiles'
export type { VisibleTile } from './tiles'

export type {
  AnnotationResult,
  AttributeValue,
  BBox,
  BBoxShape,
  ItemTiles,
  LabelAttribute,
  LabelClass,
  MaskShape,
  MediaType,
  Point2D,
  PointShape,
  PolygonShape,
  PolylineShape,
  Shape,
  ShapeAttributes,
  ShapeTool,
  SmartPrompt,
  TileKey,
  Tool,
} from './types'
