/**
 * The shared track under a waveform or a plot (§5): segments as coloured
 * bands, a drag across the track creates one, a click on a band selects it.
 * What is drawn under the bands (`children`) is the annotator's business.
 * Everything a drag does can also be done from the segment list, so the
 * timeline is a pointer convenience, not the only way in (UX-7).
 */

import { useRef, useState } from 'react'
import type { PointerEvent, ReactNode } from 'react'

import type { LabelClass, SegmentShape } from '@/api/types'

import { positionToPercent, xToPosition } from './segments'

export interface SegmentTimelineProps {
  min: number
  max: number
  segments: SegmentShape[]
  classes: LabelClass[]
  selectedId: string | null
  readOnly?: boolean
  /** Playback position, drawn as a line (audio). */
  playhead?: number | null
  /** A drag finished between these two positions (unordered, unclamped). */
  onCreate: (a: number, b: number) => void
  onSelect: (id: string) => void
  /** A click without a drag: e.g. seek the audio there. */
  onSeek?: (position: number) => void
  label: string
  height?: number
  children?: ReactNode
}

/** Pixels a pointer must move before a press becomes a drag. */
const DRAG_THRESHOLD_PX = 4

export function SegmentTimeline({
  min,
  max,
  segments,
  classes,
  selectedId,
  readOnly = false,
  playhead = null,
  onCreate,
  onSelect,
  onSeek,
  label,
  height = 120,
  children,
}: SegmentTimelineProps): JSX.Element {
  const trackRef = useRef<HTMLDivElement>(null)
  const [drag, setDrag] = useState<{ fromX: number; toX: number } | null>(null)
  const colours = new Map(classes.map((cls) => [cls.name, cls.color]))

  function localX(event: PointerEvent): number {
    const rect = trackRef.current?.getBoundingClientRect()
    return rect ? event.clientX - rect.left : 0
  }

  function width(): number {
    return trackRef.current?.getBoundingClientRect().width ?? 0
  }

  function onPointerDown(event: PointerEvent<HTMLDivElement>): void {
    if (event.button !== 0 || (event.target as HTMLElement).dataset.segmentId) return
    event.currentTarget.setPointerCapture?.(event.pointerId)
    const x = localX(event)
    setDrag({ fromX: x, toX: x })
  }

  function onPointerMove(event: PointerEvent<HTMLDivElement>): void {
    if (drag) setDrag({ ...drag, toX: localX(event) })
  }

  function onPointerUp(event: PointerEvent<HTMLDivElement>): void {
    if (!drag) return
    const toX = localX(event)
    const w = width()
    if (Math.abs(toX - drag.fromX) >= DRAG_THRESHOLD_PX && !readOnly) {
      onCreate(xToPosition(drag.fromX, w, min, max), xToPosition(toX, w, min, max))
    } else {
      onSeek?.(xToPosition(toX, w, min, max))
    }
    setDrag(null)
  }

  const dragLeft = drag ? Math.min(drag.fromX, drag.toX) : 0
  const dragWidth = drag ? Math.abs(drag.toX - drag.fromX) : 0

  return (
    <div
      ref={trackRef}
      role="group"
      aria-label={label}
      className="relative w-full cursor-crosshair select-none overflow-hidden rounded-md border border-line bg-surface"
      style={{ height }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={() => setDrag(null)}
    >
      <div className="pointer-events-none absolute inset-0">{children}</div>
      {segments.map((segment) => {
        const left = positionToPercent(segment.start, min, max)
        const right = positionToPercent(segment.end, min, max)
        const selected = segment.id === selectedId
        return (
          <button
            key={segment.id}
            type="button"
            data-segment-id={segment.id}
            aria-pressed={selected}
            aria-label={`${segment.class} ${segment.start}–${segment.end}`}
            onClick={() => onSelect(segment.id)}
            className={`absolute top-0 h-full border-x-2 opacity-40 hover:opacity-60 ${
              selected ? 'opacity-70 outline outline-2 outline-accent' : ''
            }`}
            style={{
              left: `${left}%`,
              width: `${Math.max(0.3, right - left)}%`,
              backgroundColor: colours.get(segment.class) ?? '#64748b',
              borderColor: colours.get(segment.class) ?? '#64748b',
            }}
          />
        )
      })}
      {drag && dragWidth > 0 && (
        <div
          className="pointer-events-none absolute top-0 h-full bg-accent/30"
          style={{ left: dragLeft, width: dragWidth }}
        />
      )}
      {playhead !== null && playhead >= min && playhead <= max && (
        <div
          className="pointer-events-none absolute top-0 h-full w-px bg-danger"
          style={{ left: `${positionToPercent(playhead, min, max)}%` }}
        />
      )}
    </div>
  )
}
