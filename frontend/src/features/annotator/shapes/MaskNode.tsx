/**
 * Renders a `mask` shape: the RLE is decoded once per change into a canvas
 * cropped to the mask's bounds and drawn as a Konva image, so a small mask on
 * a large image costs only its own pixels. Selected masks get their bounds
 * outlined. Hit detection is the bounds rectangle (Konva's default for images).
 */

import { memo, useMemo } from 'react'
import { Group, Image as KonvaImage, Label, Rect, Tag, Text } from 'react-konva'

import { hexToRgb, maskPixels, rleBounds } from '../mask'
import type { MaskShape } from '../types'

const FILL_ALPHA = 110 // of 255
const SELECTED_FILL_ALPHA = 160

export interface MaskNodeProps {
  shape: MaskShape
  color: string
  scale: number
  selected: boolean
  showLabel: boolean
  onSelect?: (id: string) => void
  listening?: boolean
}

function MaskNodeImpl({
  shape,
  color,
  scale,
  selected,
  showLabel,
  onSelect,
  listening = true,
}: MaskNodeProps) {
  const bounds = useMemo(() => rleBounds(shape.rle), [shape.rle])
  const canvas = useMemo(() => {
    if (!bounds || typeof document === 'undefined') return null
    const [x0, y0, x1, y1] = bounds
    const element = document.createElement('canvas')
    element.width = x1 - x0
    element.height = y1 - y0
    const context = element.getContext('2d')
    if (!context) return null
    const alpha = selected ? SELECTED_FILL_ALPHA : FILL_ALPHA
    const pixels = maskPixels(shape.rle, bounds, hexToRgb(color), alpha)
    context.putImageData(new ImageData(pixels, element.width, element.height), 0, 0)
    return element
  }, [bounds, color, selected, shape.rle])

  if (!bounds) return null
  const [x0, y0, x1, y1] = bounds
  const select = onSelect ? () => onSelect(shape.id) : undefined

  return (
    <Group>
      {canvas && (
        <KonvaImage
          image={canvas}
          x={x0}
          y={y0}
          width={x1 - x0}
          height={y1 - y0}
          listening={listening}
          perfectDrawEnabled={false}
          onMouseDown={select}
          onTap={select}
        />
      )}
      {selected && (
        <Rect
          x={x0}
          y={y0}
          width={x1 - x0}
          height={y1 - y0}
          stroke={color}
          strokeWidth={1.5 / scale}
          dash={[6 / scale, 4 / scale]}
          listening={false}
          perfectDrawEnabled={false}
        />
      )}
      {showLabel && (
        <Label x={x0} y={y0} listening={false} scaleX={1 / scale} scaleY={1 / scale}>
          <Tag fill={color} cornerRadius={2} />
          <Text text={shape.class} fontSize={11} padding={3} fill="#ffffff" />
        </Label>
      )}
    </Group>
  )
}

export const MaskNode = memo(MaskNodeImpl)
