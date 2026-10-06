import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import type { ReactNode } from 'react'
import { beforeAll, describe, expect, it, vi } from 'vitest'

import type { AnnotationResult, LabelClass, SegmentShape } from '@/api/types'
import { AudioAnnotator } from '@/features/audio-annotator'
import { TimeSeriesAnnotator } from '@/features/timeseries-annotator'

import { parseSeries } from './series'

const SPEECH: LabelClass = {
  name: 'speech',
  display_name: 'Speech',
  color: '#2563eb',
  tools: ['segment'],
  attributes: [],
}
const NOISE: LabelClass = { ...SPEECH, name: 'noise', display_name: 'Noise', color: '#d97706' }

let latest: AnnotationResult | null = null

beforeAll(() => {
  // jsdom draws nothing; the waveform canvas copes with no context.
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
})

function wrap(node: ReactNode): JSX.Element {
  return <QueryClientProvider client={new QueryClient()}>{node}</QueryClientProvider>
}

function AudioHarness({ initial, readOnly }: { initial?: SegmentShape[]; readOnly?: boolean }): JSX.Element {
  const [value, setValue] = useState<AnnotationResult>({
    schema_version: 1,
    media_type: 'audio',
    classification: {},
    shapes: initial ?? [],
  })
  latest = value
  return wrap(
    <AudioAnnotator
      mediaUrl="https://storage.example/a.wav"
      classes={[SPEECH, NOISE]}
      value={value}
      onChange={setValue}
      readOnly={readOnly}
      waveform={{ peaks: [[-0.5, 0.5]], durationMs: 10_000 }}
    />,
  )
}

function segments(): SegmentShape[] {
  return (latest?.shapes ?? []).filter((s): s is SegmentShape => s.type === 'segment')
}

/** jsdom has no layout: give the timeline a 1000 px wide box at x = 0. */
function layOut(element: HTMLElement): void {
  element.getBoundingClientRect = () =>
    ({ left: 0, top: 0, width: 1000, height: 120, right: 1000, bottom: 120, x: 0, y: 0 }) as DOMRect
}

/** jsdom has no PointerEvent: mouse events of the pointer types carry the coordinates. */
function pointer(element: HTMLElement, type: string, clientX: number): void {
  fireEvent(element, new MouseEvent(type, { bubbles: true, button: 0, clientX }))
}

function drag(element: HTMLElement, fromX: number, toX: number): void {
  pointer(element, 'pointerdown', fromX)
  pointer(element, 'pointermove', toX)
  pointer(element, 'pointerup', toX)
}

describe('AudioAnnotator', () => {
  it('marks a segment by dragging across the waveform, in whole milliseconds', () => {
    render(<AudioHarness />)
    const timeline = screen.getByRole('group', { name: /Timeline/ })
    layOut(timeline)
    drag(timeline, 250, 105.07)

    const [segment] = segments()
    expect(segment).toMatchObject({ class: 'speech', start: 1051, end: 2500, confidence: null })
  })

  it('adds, edits and removes a segment from the keyboard path', async () => {
    const user = userEvent.setup()
    render(<AudioHarness />)

    await user.selectOptions(screen.getByLabelText('New segments:'), 'noise')
    await user.click(screen.getByRole('button', { name: 'Add segment at playhead' }))
    expect(segments()).toMatchObject([{ class: 'noise', start: 0, end: 2000 }])

    await user.type(screen.getByLabelText('Segment 1: Speaker'), 'Anna')
    await user.type(screen.getByLabelText('Segment 1: Transcript'), 'Hei')
    const end = screen.getByLabelText('Segment 1: End')
    await user.clear(end)
    await user.type(end, '3.25{Enter}')
    expect(segments()[0]).toMatchObject({ speaker: 'Anna', text: 'Hei', end: 3250 })

    // A start past the end moves the whole segment; an end before the start is reverted.
    const start = screen.getByLabelText('Segment 1: Start')
    await user.clear(start)
    await user.type(start, '5')
    await user.tab()
    expect(segments()[0]).toMatchObject({ start: 5000, end: 8250 })
    const endAgain = screen.getByLabelText('Segment 1: End')
    await user.clear(endAgain)
    await user.type(endAgain, '1{Enter}')
    expect(segments()[0]).toMatchObject({ start: 5000, end: 8250 })
    expect(screen.getByLabelText('Segment 1: End')).toHaveValue(8.25)

    await user.click(screen.getByRole('button', { name: 'Remove Segment 1' }))
    expect(segments()).toEqual([])
  })

  it('uses the first segment class when the schema arrives after the first render', () => {
    function Late(): JSX.Element {
      const [classes, setClasses] = useState<LabelClass[]>([])
      const [value, setValue] = useState<AnnotationResult>({
        schema_version: 1,
        media_type: 'audio',
        classification: {},
        shapes: [],
      })
      latest = value
      return wrap(
        <>
          <button type="button" onClick={() => setClasses([NOISE])}>
            load schema
          </button>
          <AudioAnnotator
            mediaUrl="https://storage.example/a.wav"
            classes={classes}
            value={value}
            onChange={setValue}
            waveform={{ peaks: [], durationMs: 1000 }}
          />
        </>,
      )
    }
    render(<Late />)
    fireEvent.click(screen.getByRole('button', { name: 'load schema' }))
    const timeline = screen.getByRole('group', { name: /Timeline/ })
    layOut(timeline)
    drag(timeline, 100, 300)
    expect(segments()).toMatchObject([{ class: 'noise', start: 100, end: 300 }])
  })

  it('keeps focus when tabbing from start to end, even when the start moves the segment', async () => {
    const user = userEvent.setup()
    render(<AudioHarness />)
    await user.click(screen.getByRole('button', { name: 'Add segment at playhead' }))

    const start = screen.getByLabelText('Segment 1: Start')
    await user.clear(start)
    await user.type(start, '4')
    await user.tab()
    const end = screen.getByLabelText('Segment 1: End')
    expect(end).toHaveFocus()
    expect(end).toHaveValue(6)
    await user.clear(end)
    await user.type(end, '5.5')
    await user.tab()
    expect(segments()[0]).toMatchObject({ start: 4000, end: 5500 })
  })

  it('is read-only on the review page', () => {
    render(
      <AudioHarness
        readOnly
        initial={[
          {
            id: 's1',
            type: 'segment',
            class: 'speech',
            attributes: {},
            confidence: null,
            start: 0,
            end: 500,
          },
        ]}
      />,
    )
    expect(screen.queryByRole('button', { name: 'Add segment at playhead' })).toBeNull()
    expect(screen.getByLabelText('Segment 1: Speaker')).toBeDisabled()
    const timeline = screen.getByRole('group', { name: /Timeline/ })
    layOut(timeline)
    drag(timeline, 600, 900)
    expect(segments()).toHaveLength(1)
  })
})

const CSV = 't,x,y\n0,1,5\n1,2,4\n2,3,3\n3,2,2\n4,1,1\n'

function SeriesHarness(): JSX.Element {
  const [value, setValue] = useState<AnnotationResult>({
    schema_version: 1,
    media_type: 'timeseries',
    classification: {},
    shapes: [],
  })
  latest = value
  const parsed = parseSeries(CSV)
  if (!parsed.ok) throw new Error(parsed.error)
  return (
    <TimeSeriesAnnotator series={parsed.series} classes={[SPEECH]} value={value} onChange={setValue} />
  )
}

describe('TimeSeriesAnnotator', () => {
  it('marks an interval on the time axis covering all channels', () => {
    render(<SeriesHarness />)
    const timeline = screen.getByRole('group', { name: /Timeline/ })
    layOut(timeline)
    drag(timeline, 250, 500)
    expect(segments()).toMatchObject([{ start: 1, end: 2, channels: null }])
  })

  it('covers only the shown channels when asked, and edits channels in the list', async () => {
    const user = userEvent.setup()
    render(<SeriesHarness />)
    await user.click(screen.getByRole('checkbox', { name: 'y' }))
    await user.selectOptions(screen.getByLabelText('New segments cover:'), 'shown')
    await user.click(screen.getByRole('button', { name: 'Add segment for the selection' }))
    expect(segments()).toMatchObject([{ start: 0, end: 0.4, channels: ['x'] }])

    const row = screen.getByRole('list', { name: 'Segments' })
    await user.click(within(row).getByRole('checkbox', { name: 'y' }))
    expect(segments()[0]?.channels).toBeNull()
  })
})
