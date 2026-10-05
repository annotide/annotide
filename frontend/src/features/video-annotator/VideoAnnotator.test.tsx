import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { AnnotationResult, BBoxShape, LabelClass } from '@/api/types'

import { VideoAnnotator } from './VideoAnnotator'

const CLASSES: LabelClass[] = [
  { name: 'car', display_name: 'Car', color: '#f00', tools: ['bbox'], attributes: [] },
]

const EMPTY: AnnotationResult = {
  schema_version: 1,
  media_type: 'video',
  classification: {},
  shapes: [],
}

function Harness({ initial = EMPTY }: { initial?: AnnotationResult }): JSX.Element {
  const [value, setValue] = useState<AnnotationResult>(initial)
  return (
    <>
      <VideoAnnotator
        videoUrl="https://media.example/clip.mp4"
        width={100}
        height={100}
        fps={10}
        classes={CLASSES}
        value={value}
        onChange={setValue}
      />
      <output data-testid="value">{JSON.stringify(value.shapes)}</output>
    </>
  )
}

function shapes(): BBoxShape[] {
  return JSON.parse(screen.getByTestId('value').textContent ?? '[]') as BBoxShape[]
}

function seekVideo(time: number): void {
  const video = screen.getByTestId('video-element') as HTMLVideoElement
  Object.defineProperty(video, 'currentTime', { value: time, writable: true, configurable: true })
  fireEvent(video, new Event('timeupdate'))
}

beforeEach(() => {
  // jsdom doesn't implement media playback.
  HTMLMediaElement.prototype.play = vi.fn().mockResolvedValue(undefined)
  HTMLMediaElement.prototype.pause = vi.fn()
  vi.spyOn(SVGSVGElement.prototype, 'getBoundingClientRect').mockReturnValue({
    left: 0,
    top: 0,
    width: 100,
    height: 100,
    right: 100,
    bottom: 100,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  })
})

function drag(from: [number, number], to: [number, number]): void {
  const overlay = screen.getByTestId('video-overlay')
  fireEvent.mouseDown(overlay, { clientX: from[0], clientY: from[1] })
  fireEvent.mouseMove(window, { clientX: to[0], clientY: to[1] })
  fireEvent.mouseUp(window, { clientX: to[0], clientY: to[1] })
}

describe('VideoAnnotator', () => {
  it('shows the frame computed from currentTime and fps', () => {
    render(<Harness />)
    seekVideo(1.05)
    expect(screen.getByTestId('frame-counter')).toHaveTextContent('Frame 10')
  })

  it('drawing a box creates a track with a keyframe at the current frame', () => {
    render(<Harness />)
    seekVideo(0)
    drag([10, 10], [30, 30])

    const created = shapes()
    expect(created).toHaveLength(1)
    expect(created[0].type).toBe('bbox')
    expect(created[0].frame).toBe(0)
    expect(created[0].track_id).toBeTruthy()
    expect(created[0].keyframe).toBe(true)
    expect(created[0].bbox).toEqual([10, 10, 30, 30])
  })

  it('moving the selected box on another frame adds a second keyframe and interpolates between them', () => {
    render(<Harness />)
    seekVideo(0)
    drag([10, 10], [30, 30])
    const trackId = shapes()[0].track_id

    // Move to frame 10 and drag the box to a new position: a second keyframe.
    seekVideo(1)
    const box = screen.getByTestId(`track-box-${trackId}`)
    fireEvent.mouseDown(box, { clientX: 20, clientY: 20 })
    fireEvent.mouseMove(window, { clientX: 40, clientY: 40 })
    fireEvent.mouseUp(window, { clientX: 40, clientY: 40 })

    const withTwo = shapes()
    expect(withTwo).toHaveLength(2)
    const byFrame = [...withTwo].sort((a, b) => (a.frame ?? 0) - (b.frame ?? 0))
    expect(byFrame[0].frame).toBe(0)
    expect(byFrame[1].frame).toBe(10)
    expect(byFrame[1].bbox).toEqual([30, 30, 50, 50])

    // Halfway (frame 5) shows an interpolated, dashed box.
    seekVideo(0.5)
    const interpolated = screen.getByTestId(`track-box-${trackId}`)
    expect(interpolated).toHaveAttribute('stroke-dasharray')
    expect(interpolated.getAttribute('x')).toBe('20')
    expect(interpolated.getAttribute('y')).toBe('20')
  })

  it('marking a track outside hides it until the next keyframe', () => {
    render(<Harness />)
    seekVideo(0)
    drag([10, 10], [30, 30])
    const trackId = shapes()[0].track_id as string

    seekVideo(1)
    const box = screen.getByTestId(`track-box-${trackId}`)
    fireEvent.mouseDown(box, { clientX: 20, clientY: 20 })
    fireEvent.mouseUp(window, { clientX: 20, clientY: 20 })

    fireEvent.click(screen.getByRole('button', { name: /outside from here/i }))

    expect(screen.queryByTestId(`track-box-${trackId}`)).not.toBeInTheDocument()

    seekVideo(0.5)
    expect(screen.getByTestId(`track-box-${trackId}`)).toBeInTheDocument()
  })

  it('does not render editing controls in read-only mode', () => {
    const shape: BBoxShape = {
      id: crypto.randomUUID(),
      type: 'bbox',
      class: 'car',
      attributes: {},
      confidence: null,
      frame: 0,
      track_id: crypto.randomUUID(),
      keyframe: true,
      outside: false,
      bbox: [1, 1, 2, 2],
    }
    render(
      <VideoAnnotator
        videoUrl="https://media.example/clip.mp4"
        width={100}
        height={100}
        fps={10}
        classes={CLASSES}
        value={{ ...EMPTY, shapes: [shape] }}
        onChange={() => {}}
        readOnly
      />,
    )
    expect(screen.queryByText('Class')).not.toBeInTheDocument()
    expect(screen.getByTestId(`track-box-${shape.track_id}`)).toBeInTheDocument()
  })
})
