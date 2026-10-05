/**
 * Drag handles for the selected shape in the select tool (post-draw editing).
 * Only draws and reports presses; the annotator owns the gesture, so the
 * handle positions always follow the previewed shape (`../edit.ts`).
 */

import type Konva from 'konva'
import { Circle, Group, Line, Rect } from 'react-konva'

import { shapeHandles } from '../edit'
import type { Shape } from '../types'

/** Handle side in screen pixels. */
const HANDLE_PX = 9
const INSERT_PX = 6

export interface EditHandlesProps {
  shape: Shape
  color: string
  scale: number
  /** A press on handle `index`; `alt` asks to remove that vertex. */
  onHandleDown: (index: number, alt: boolean) => void
}

function setCursor(event: Konva.KonvaEventObject<MouseEvent>, cursor: string): void {
  const container = event.target.getStage()?.container()
  if (container) container.style.cursor = cursor
}

export function EditHandles({ shape, color, scale, onHandleDown }: EditHandlesProps) {
  const handles = shapeHandles(shape, scale)
  const rotate = handles.find((h) => h.kind === 'rotate')
  const size = HANDLE_PX / scale
  const common = {
    stroke: color,
    strokeWidth: 1.5 / scale,
    perfectDrawEnabled: false,
    shadowForStrokeEnabled: false,
  }

  return (
    <Group>
      {rotate && shape.type === 'rbox' && (
        <Line
          {...common}
          listening={false}
          points={[shape.center[0], shape.center[1], rotate.point[0], rotate.point[1]]}
          dash={[4 / scale, 3 / scale]}
        />
      )}
      {handles.map(({ index, kind, point: [x, y] }) => {
        const events = {
          onMouseDown: (event: Konva.KonvaEventObject<MouseEvent>) =>
            onHandleDown(index, event.evt.altKey),
          onMouseEnter: (event: Konva.KonvaEventObject<MouseEvent>) =>
            setCursor(event, kind === 'rotate' ? 'grab' : 'pointer'),
          onMouseLeave: (event: Konva.KonvaEventObject<MouseEvent>) => setCursor(event, ''),
        }
        if (kind === 'vertex') {
          return (
            <Rect
              key={index}
              {...common}
              {...events}
              name="edit-handle"
              x={x - size / 2}
              y={y - size / 2}
              width={size}
              height={size}
              fill="#ffffff"
            />
          )
        }
        return (
          <Circle
            key={index}
            {...common}
            {...events}
            name="edit-handle"
            x={x}
            y={y}
            radius={(kind === 'insert' ? INSERT_PX : HANDLE_PX) / 2 / scale}
            fill={kind === 'insert' ? color : '#ffffff'}
            opacity={kind === 'insert' ? 0.7 : 1}
          />
        )
      })}
    </Group>
  )
}
