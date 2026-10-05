/**
 * Renders one annotation shape on the Konva layer.
 *
 * Performance notes (IMG-4 — 10 000 shapes must stay responsive):
 * - `memo` so a shape only re-renders when its own props change; the parent
 *   re-renders on every pointer move during a drag.
 * - `perfectDrawEnabled={false}` and `shadowForStrokeEnabled={false}` skip the
 *   offscreen buffer Konva otherwise uses for exact stroke rendering.
 * - `listening={false}` on unselected shapes in read-only mode, and on the
 *   label chip always, so hit detection walks far fewer nodes.
 * - Stroke width is divided by the viewport scale so outlines stay ~1.5 screen
 *   pixels at every zoom level rather than growing with the image.
 */

import { memo } from 'react'
import { Circle, Group, Label, Line, Rect, Tag, Text } from 'react-konva'

import { rboxCorners } from '../geometry'
import type { Keypoint, Shape } from '../types'
import { MaskNode } from './MaskNode'

const BASE_STROKE = 1.5
const SELECTED_STROKE = 2.5
const FILL_ALPHA = '33' // 20% in hex, appended to a #rrggbb colour
const POINT_RADIUS = 5
const KEYPOINT_RADIUS = 4

export interface ShapeNodeProps {
  shape: Shape
  color: string
  scale: number
  selected: boolean
  showLabel: boolean
  onSelect?: (id: string) => void
  listening?: boolean
  /** Bones for a `keypoints` shape: its class's skeleton edges. */
  edges?: [number, number][]
}

function ShapeNodeImpl({
  shape,
  color,
  scale,
  selected,
  showLabel,
  onSelect,
  listening = true,
  edges,
}: ShapeNodeProps) {
  const strokeWidth = (selected ? SELECTED_STROKE : BASE_STROKE) / scale
  // Model-produced shapes are drawn dashed so a human can tell at a glance what
  // still needs checking (ML-3).
  const dash = shape.confidence !== null ? [6 / scale, 4 / scale] : undefined

  const common = {
    stroke: color,
    strokeWidth,
    dash,
    perfectDrawEnabled: false,
    shadowForStrokeEnabled: false,
    listening,
    onMouseDown: onSelect ? () => onSelect(shape.id) : undefined,
    onTap: onSelect ? () => onSelect(shape.id) : undefined,
  }

  let node: JSX.Element | null = null
  let labelAt: [number, number] | null = null

  switch (shape.type) {
    case 'bbox': {
      const [xMin, yMin, xMax, yMax] = shape.bbox
      node = (
        <Rect
          {...common}
          x={xMin}
          y={yMin}
          width={xMax - xMin}
          height={yMax - yMin}
          fill={`${color}${FILL_ALPHA}`}
        />
      )
      labelAt = [xMin, yMin]
      break
    }
    case 'rbox': {
      const [width, height] = shape.size
      node = (
        <Rect
          {...common}
          x={shape.center[0]}
          y={shape.center[1]}
          offsetX={width / 2}
          offsetY={height / 2}
          width={width}
          height={height}
          rotation={shape.angle}
          fill={`${color}${FILL_ALPHA}`}
        />
      )
      labelAt = rboxCorners(shape.center, shape.size, shape.angle)[0]
      break
    }
    case 'polygon': {
      node = (
        <Line
          {...common}
          points={shape.points.flat()}
          closed
          fill={`${color}${FILL_ALPHA}`}
        />
      )
      labelAt = shape.points[0] ?? null
      break
    }
    case 'polyline': {
      node = <Line {...common} points={shape.points.flat()} />
      labelAt = shape.points[0] ?? null
      break
    }
    case 'point': {
      node = (
        <Circle
          {...common}
          x={shape.point[0]}
          y={shape.point[1]}
          radius={POINT_RADIUS / scale}
          fill={color}
        />
      )
      labelAt = shape.point
      break
    }
    case 'keypoints': {
      node = (
        <KeypointsGroup
          points={shape.points}
          edges={edges ?? []}
          common={common}
          scale={scale}
          color={color}
        />
      )
      const first = shape.points.find(([, , v]) => v > 0)
      labelAt = first ? [first[0], first[1]] : null
      break
    }
    case 'mask':
      return (
        <MaskNode
          shape={shape}
          color={color}
          scale={scale}
          selected={selected}
          showLabel={showLabel}
          onSelect={onSelect}
          listening={listening}
        />
      )
  }

  if (!node) return null

  return (
    <Group>
      {node}
      {showLabel && labelAt && (
        <Label x={labelAt[0]} y={labelAt[1]} listening={false} scaleX={1 / scale} scaleY={1 / scale}>
          <Tag fill={color} cornerRadius={2} />
          <Text text={shape.class} fontSize={11} padding={3} fill="#ffffff" />
        </Label>
      )}
    </Group>
  )
}

/** Bones between labelled points, then a dot per labelled point: filled when
 * visible, hollow when occluded (v = 1). */
function KeypointsGroup({
  points,
  edges,
  common,
  scale,
  color,
}: {
  points: Keypoint[]
  edges: [number, number][]
  common: Record<string, unknown>
  scale: number
  color: string
}) {
  return (
    <Group>
      {edges.map(([a, b]) => {
        const from = points[a]
        const to = points[b]
        if (!from || !to || from[2] === 0 || to[2] === 0) return null
        return <Line key={`${a}-${b}`} {...common} points={[from[0], from[1], to[0], to[1]]} />
      })}
      {points.map(([x, y, v], index) =>
        v === 0 ? null : (
          <Circle
            // A skeleton slot's index is its identity.
            key={index}
            {...common}
            x={x}
            y={y}
            radius={KEYPOINT_RADIUS / scale}
            fill={v === 2 ? color : undefined}
          />
        ),
      )}
    </Group>
  )
}

export const ShapeNode = memo(ShapeNodeImpl)
