import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import type { AnnotationResult, LabelClass } from '@/api/types'
import { PdfAnnotator } from './PdfAnnotator'
import type { PdfAnnotatorProps } from './PdfAnnotator'
import type { PdfDocument } from './pdf'
import { wordBoxes } from './words'

const CLASSES: LabelClass[] = [
  {
    name: 'total',
    display_name: 'Total',
    color: '#e11d48',
    tools: ['bbox'],
    attributes: [],
  },
  {
    name: 'date',
    display_name: 'Date',
    color: '#2563eb',
    tools: ['bbox'],
    attributes: [],
  },
  { name: 'stamp', display_name: 'Stamp', color: '#16a34a', tools: ['polygon'], attributes: [] },
  { name: 'sign', display_name: 'Signature', color: '#9333ea', tools: ['point'], attributes: [] },
] as LabelClass[]

// 600 × 800 pt pages; page 1 says "Total 42" near the top.
function fakeDocument(): PdfDocument {
  const words = wordBoxes(
    [{ str: 'Total 42', transform: [10, 0, 0, 10, 100, 700], width: 80 }],
    [1, 0, 0, -1, 0, 800],
  )
  return {
    pageCount: 2,
    page: vi.fn(async (n: number) => ({
      width: 600,
      height: 800,
      render: vi.fn(async () => undefined),
      words: async () => (n === 1 ? words : []),
    })),
    destroy: vi.fn(async () => undefined),
  }
}

/** Hotkeys stay off while a form field has focus. */
function blurFocus(): void {
  const focused = document.activeElement as HTMLElement | null
  focused?.blur()
}

const EMPTY: AnnotationResult = {
  schema_version: 1,
  media_type: 'pdf',
  classification: {},
  shapes: [],
}

function setup(
  value: AnnotationResult = EMPTY,
  props: Partial<Pick<PdfAnnotatorProps, 'readOnly' | 'readText'>> = {},
) {
  const onChange = vi.fn()
  const doc = fakeDocument()
  const utils = render(
    <PdfAnnotator
      pdfUrl="https://storage.example/doc.pdf"
      classes={CLASSES}
      value={value}
      onChange={onChange}
      open={async () => doc}
      {...props}
    />,
  )
  return { ...utils, onChange, doc }
}

// jsdom lays nothing out: the overlay's rect is 0 × 0, so client coordinates
// pass through as page points (see pointer.ts).
function drag(overlay: Element, from: [number, number], to: [number, number]): void {
  fireEvent.mouseDown(overlay, { clientX: from[0], clientY: from[1] })
  fireEvent.mouseMove(overlay, { clientX: to[0], clientY: to[1] })
  fireEvent.mouseUp(overlay, { clientX: to[0], clientY: to[1] })
}

describe('PdfAnnotator', () => {
  it('draws a box on the current page and records the text under it', async () => {
    const { onChange } = setup()
    const overlay = await screen.findByTestId('pdf-overlay')
    expect(screen.getByTestId('page-counter')).toHaveTextContent('Page 1 / 2')

    drag(overlay, [95, 85], [185, 105])

    expect(onChange).toHaveBeenCalledTimes(1)
    const next = onChange.mock.calls[0][0] as AnnotationResult
    expect(next.media_type).toBe('pdf')
    expect(next.shapes).toEqual([
      expect.objectContaining({
        type: 'bbox',
        class: 'total',
        page: 1,
        bbox: [95, 85, 185, 105],
        text: 'Total 42',
        confidence: null,
      }),
    ])
  })

  it('leaves text out where the page has none, and ignores clicks', async () => {
    const { onChange } = setup()
    const overlay = await screen.findByTestId('pdf-overlay')
    drag(overlay, [10, 10], [11, 11])
    expect(onChange).not.toHaveBeenCalled()

    await userEvent.selectOptions(screen.getByRole('combobox'), 'date')
    drag(overlay, [300, 300], [400, 350])
    const shape = (onChange.mock.calls[0][0] as AnnotationResult).shapes[0]
    expect(shape).toMatchObject({ class: 'date', page: 1 })
    expect(shape).not.toHaveProperty('text')
  })

  it('shows each page its own shapes', async () => {
    const value: AnnotationResult = {
      ...EMPTY,
      shapes: [
        {
          id: 'a',
          type: 'bbox',
          class: 'total',
          attributes: {},
          confidence: null,
          page: 1,
          bbox: [1, 1, 20, 20],
        },
        {
          id: 'b',
          type: 'bbox',
          class: 'date',
          attributes: {},
          confidence: null,
          page: 2,
          bbox: [1, 1, 20, 20],
        },
      ],
    }
    setup(value)
    expect(await screen.findByTestId('pdf-box-a')).toBeInTheDocument()
    expect(screen.queryByTestId('pdf-box-b')).toBeNull()
    expect(screen.getByRole('button', { name: 'Previous page' })).toBeDisabled()

    await userEvent.click(screen.getByRole('button', { name: 'Next page' }))

    expect(await screen.findByTestId('pdf-box-b')).toBeInTheDocument()
    expect(screen.queryByTestId('pdf-box-a')).toBeNull()
    expect(screen.getByTestId('page-counter')).toHaveTextContent('Page 2 / 2')
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled()
  })

  it('selects, re-classes and deletes a box', async () => {
    const value: AnnotationResult = {
      ...EMPTY,
      shapes: [
        {
          id: 'a',
          type: 'bbox',
          class: 'total',
          attributes: {},
          confidence: null,
          page: 1,
          bbox: [1, 1, 20, 20],
          text: '42',
        },
      ],
    }
    const { onChange } = setup(value)
    fireEvent.mouseDown(await screen.findByTestId('pdf-box-a'))
    expect(screen.getByTestId('pdf-selection-text')).toHaveTextContent('42')

    await userEvent.selectOptions(
      screen.getByRole('combobox', { name: 'Class of the selected box' }),
      'date',
    )
    expect((onChange.mock.calls.at(-1)?.[0] as AnnotationResult).shapes[0].class).toBe('date')

    // Hotkeys stay off while a form field has focus.
    blurFocus()
    await userEvent.keyboard('{Delete}')
    expect((onChange.mock.calls.at(-1)?.[0] as AnnotationResult).shapes).toEqual([])
  })

  it('moves a box and re-reads the words under it', async () => {
    const value: AnnotationResult = {
      ...EMPTY,
      shapes: [
        {
          id: 'b1',
          type: 'bbox',
          class: 'total',
          attributes: {},
          confidence: null,
          page: 1,
          bbox: [295, 285, 385, 305],
        },
      ],
    }
    const { onChange } = setup(value)
    const overlay = await screen.findByTestId('pdf-overlay')

    // Mouse down on the box, released 200 pt up-left: over "Total 42".
    fireEvent.mouseDown(await screen.findByTestId('pdf-box-b1'), { clientX: 300, clientY: 290 })
    fireEvent.mouseMove(overlay, { clientX: 100, clientY: 90 })
    // The preview follows the pointer before anything is committed.
    expect(screen.getByTestId('pdf-box-b1')).toHaveAttribute('x', '95')
    expect(onChange).not.toHaveBeenCalled()
    fireEvent.mouseUp(overlay, { clientX: 100, clientY: 90 })

    expect(onChange).toHaveBeenCalledTimes(1)
    expect((onChange.mock.calls[0][0] as AnnotationResult).shapes).toEqual([
      expect.objectContaining({ id: 'b1', bbox: [95, 85, 185, 105], text: 'Total 42' }),
    ])
  })

  it('resizes a box by a handle, drops text it no longer covers, and ignores a click', async () => {
    const value: AnnotationResult = {
      ...EMPTY,
      shapes: [
        {
          id: 'b1',
          type: 'bbox',
          class: 'total',
          attributes: {},
          confidence: null,
          page: 1,
          bbox: [95, 85, 185, 105],
          text: 'Total 42',
        },
      ],
    }
    const { onChange } = setup(value)
    const overlay = await screen.findByTestId('pdf-overlay')
    const box = await screen.findByTestId('pdf-box-b1')

    // A click selects without changing anything.
    fireEvent.mouseDown(box, { clientX: 120, clientY: 95 })
    fireEvent.mouseUp(overlay, { clientX: 120, clientY: 95 })
    expect(onChange).not.toHaveBeenCalled()

    // Handle 1 is the north-east corner; pull it left past the words.
    fireEvent.mouseDown(screen.getByTestId('pdf-handle-1'), { clientX: 185, clientY: 85 })
    fireEvent.mouseMove(overlay, { clientX: 99, clientY: 60 })
    fireEvent.mouseUp(overlay, { clientX: 99, clientY: 60 })

    const [shape] = (onChange.mock.calls[0][0] as AnnotationResult).shapes
    expect(shape).toMatchObject({ id: 'b1', bbox: [95, 60, 99, 105] })
    expect(shape).not.toHaveProperty('text')
  })

  it('moves polygon vertices, adds one on an edge and removes one with Alt+click', async () => {
    const value: AnnotationResult = {
      ...EMPTY,
      shapes: [
        {
          id: 'p1',
          type: 'polygon',
          class: 'stamp',
          attributes: {},
          confidence: null,
          page: 1,
          points: [
            [10, 10],
            [100, 10],
            [100, 80],
            [10, 80],
          ],
        },
      ],
    }
    const { onChange } = setup(value)
    const overlay = await screen.findByTestId('pdf-overlay')
    fireEvent.mouseDown(await screen.findByTestId('pdf-polygon-p1'), { clientX: 50, clientY: 50 })
    fireEvent.mouseUp(overlay, { clientX: 50, clientY: 50 })

    // Vertices are handles 0-3, edge midpoints 4-7.
    fireEvent.mouseDown(screen.getByTestId('pdf-handle-2'), { clientX: 100, clientY: 80 })
    fireEvent.mouseMove(overlay, { clientX: 120, clientY: 90 })
    fireEvent.mouseUp(overlay, { clientX: 120, clientY: 90 })
    expect((onChange.mock.calls.at(-1)?.[0] as AnnotationResult).shapes[0]).toMatchObject({
      points: [
        [10, 10],
        [100, 10],
        [120, 90],
        [10, 80],
      ],
    })

    fireEvent.mouseDown(screen.getByTestId('pdf-handle-4'), { clientX: 55, clientY: 10 })
    fireEvent.mouseMove(overlay, { clientX: 55, clientY: 0 })
    fireEvent.mouseUp(overlay, { clientX: 55, clientY: 0 })
    expect((onChange.mock.calls.at(-1)?.[0] as AnnotationResult).shapes[0]).toMatchObject({
      points: [
        [10, 10],
        [55, 0],
        [100, 10],
        [100, 80],
        [10, 80],
      ],
    })

    fireEvent.mouseDown(screen.getByTestId('pdf-handle-0'), { altKey: true })
    expect((onChange.mock.calls.at(-1)?.[0] as AnnotationResult).shapes[0]).toMatchObject({
      points: [
        [100, 10],
        [100, 80],
        [10, 80],
      ],
    })
  })

  it('moves a point, kept on the page', async () => {
    const value: AnnotationResult = {
      ...EMPTY,
      shapes: [
        {
          id: 'x1',
          type: 'point',
          class: 'sign',
          attributes: {},
          confidence: null,
          page: 1,
          point: [590, 400],
        },
      ],
    }
    const { onChange } = setup(value)
    const overlay = await screen.findByTestId('pdf-overlay')
    fireEvent.mouseDown(await screen.findByTestId('pdf-point-x1'), { clientX: 590, clientY: 400 })
    fireEvent.mouseMove(overlay, { clientX: 640, clientY: 420 })
    fireEvent.mouseUp(overlay, { clientX: 640, clientY: 420 })
    expect((onChange.mock.calls[0][0] as AnnotationResult).shapes[0]).toMatchObject({
      point: [600, 420],
    })
  })

  it('neither moves shapes nor shows handles when read-only', async () => {
    const value: AnnotationResult = {
      ...EMPTY,
      shapes: [
        {
          id: 'b1',
          type: 'bbox',
          class: 'total',
          attributes: {},
          confidence: null,
          page: 1,
          bbox: [95, 85, 185, 105],
        },
      ],
    }
    const { onChange } = setup(value, { readOnly: true })
    const overlay = await screen.findByTestId('pdf-overlay')
    fireEvent.mouseDown(await screen.findByTestId('pdf-box-b1'), { clientX: 100, clientY: 90 })
    fireEvent.mouseMove(overlay, { clientX: 300, clientY: 300 })
    fireEvent.mouseUp(overlay, { clientX: 300, clientY: 300 })
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByTestId('pdf-selection')).toBeInTheDocument()
    expect(screen.queryByTestId('pdf-handle-0')).toBeNull()
  })

  describe('OCR on a scanned page', () => {
    // Page 2 of the fake document has no text layer, like a scan.
    const OCR_WORDS = [
      { text: 'Total', bbox: [300, 300, 340, 312] as [number, number, number, number] },
      { text: '42,00', bbox: [345, 300, 380, 312] as [number, number, number, number] },
    ]
    const SCANNED: AnnotationResult = {
      ...EMPTY,
      shapes: [
        {
          id: 'b2',
          type: 'bbox',
          class: 'total',
          attributes: {},
          confidence: null,
          page: 2,
          bbox: [290, 295, 390, 315],
        },
      ],
    }

    async function toPage2(): Promise<void> {
      await screen.findByTestId('pdf-overlay')
      await userEvent.click(screen.getByRole('button', { name: 'Next page' }))
      await screen.findByText('Page 2 / 2')
    }

    it('reads the page, fills earlier boxes and gives new boxes their words', async () => {
      const readText = vi.fn(async () => OCR_WORDS)
      const { onChange } = setup(SCANNED, { readText })
      // Page 1 has a text layer: nothing to offer there.
      await screen.findByTestId('pdf-overlay')
      expect(screen.queryByRole('button', { name: 'Read text (OCR)' })).toBeNull()

      await toPage2()
      await userEvent.click(await screen.findByRole('button', { name: 'Read text (OCR)' }))

      expect(readText).toHaveBeenCalledWith(2)
      expect(await screen.findByTestId('pdf-ocr-done')).toHaveTextContent(
        'Text read by OCR: 2 words',
      )
      expect(onChange).toHaveBeenCalledTimes(1)
      expect((onChange.mock.calls[0][0] as AnnotationResult).shapes[0]).toMatchObject({
        id: 'b2',
        text: 'Total 42,00',
      })

      // A new box around one OCR'd word carries it, as with a text layer.
      drag(screen.getByTestId('pdf-overlay'), [342, 295], [385, 318])
      expect((onChange.mock.calls.at(-1)?.[0] as AnnotationResult).shapes.at(-1)).toMatchObject({
        page: 2,
        text: '42,00',
      })
    })

    it('shows a failure and lets the person try again', async () => {
      const readText = vi
        .fn<(page: number) => Promise<typeof OCR_WORDS>>()
        .mockRejectedValueOnce(new Error('The model did not answer.'))
        .mockResolvedValueOnce(OCR_WORDS)
      setup(SCANNED, { readText })
      await toPage2()
      await userEvent.click(await screen.findByRole('button', { name: 'Read text (OCR)' }))
      expect(await screen.findByRole('alert')).toHaveTextContent(
        'OCR failed: The model did not answer.',
      )
      await userEvent.click(screen.getByRole('button', { name: 'Read text (OCR)' }))
      expect(await screen.findByTestId('pdf-ocr-done')).toBeInTheDocument()
      expect(screen.queryByRole('alert')).toBeNull()
    })

    it('keeps a late answer to its own page', async () => {
      let answer: (words: typeof OCR_WORDS) => void = () => undefined
      const readText = vi.fn(
        () =>
          new Promise<typeof OCR_WORDS>((resolve) => {
            answer = resolve
          }),
      )
      setup(SCANNED, { readText })
      await toPage2()
      await userEvent.click(await screen.findByRole('button', { name: 'Read text (OCR)' }))
      expect(screen.getByRole('button', { name: 'Reading the page…' })).toBeDisabled()

      await userEvent.click(screen.getByRole('button', { name: 'Previous page' }))
      await screen.findByText('Page 1 / 2')
      await act(async () => answer(OCR_WORDS))
      expect(screen.queryByTestId('pdf-ocr-done')).toBeNull()

      // Back on page 2 the words are there, read once.
      await userEvent.click(screen.getByRole('button', { name: 'Next page' }))
      expect(await screen.findByTestId('pdf-ocr-done')).toHaveTextContent('2 words')
      expect(readText).toHaveBeenCalledTimes(1)
    })

    it('offers nothing without an ocr model or when read-only', async () => {
      const { unmount } = setup(SCANNED)
      await toPage2()
      expect(screen.queryByRole('button', { name: 'Read text (OCR)' })).toBeNull()
      unmount()

      setup(SCANNED, { readOnly: true, readText: vi.fn() })
      await toPage2()
      expect(screen.queryByRole('button', { name: 'Read text (OCR)' })).toBeNull()
    })
  })

  it('draws nothing when read-only', async () => {
    const { onChange } = setup(EMPTY, { readOnly: true })
    drag(await screen.findByTestId('pdf-overlay'), [10, 10], [100, 100])
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.queryByRole('combobox')).toBeNull()
  })

  it('explains a failed load and closes the document on unmount', async () => {
    render(
      <PdfAnnotator
        pdfUrl="https://storage.example/missing.pdf"
        classes={CLASSES}
        value={EMPTY}
        onChange={vi.fn()}
        open={async () => {
          throw new Error('Failed to fetch')
        }}
      />,
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Could not open the PDF: Failed to fetch',
    )

    const { doc, unmount } = setup()
    await screen.findAllByTestId('pdf-overlay')
    unmount()
    await waitFor(() => expect(doc.destroy).toHaveBeenCalled())
  })

  it('draws a polygon on the page, closed on its first vertex or with Enter', async () => {
    const { onChange } = setup()
    const overlay = await screen.findByTestId('pdf-overlay')
    await userEvent.click(screen.getByRole('button', { name: 'Polygon' }))
    expect(screen.getByRole('combobox')).toHaveValue('stamp')

    for (const [x, y] of [
      [10, 10],
      [100, 10],
      [100, 80],
    ] as const) {
      fireEvent.mouseDown(overlay, { clientX: x, clientY: y })
    }
    expect(screen.getByTestId('pdf-polygon-draft')).toBeInTheDocument()
    fireEvent.mouseDown(overlay, { clientX: 12, clientY: 11 })

    const polygon = (onChange.mock.calls.at(-1)?.[0] as AnnotationResult).shapes[0]
    expect(polygon).toMatchObject({
      type: 'polygon',
      class: 'stamp',
      page: 1,
      points: [
        [10, 10],
        [100, 10],
        [100, 80],
      ],
    })
    expect(screen.queryByTestId('pdf-polygon-draft')).toBeNull()

    onChange.mockClear()
    for (const [x, y] of [
      [200, 200],
      [300, 200],
      [250, 260],
    ] as const) {
      fireEvent.mouseDown(overlay, { clientX: x, clientY: y })
    }
    blurFocus()
    await userEvent.keyboard('{Enter}')
    expect((onChange.mock.calls.at(-1)?.[0] as AnnotationResult).shapes.at(-1)).toMatchObject({
      type: 'polygon',
    })
  })

  it('cancels a polygon with Escape and places points with a click', async () => {
    const { onChange } = setup()
    const overlay = await screen.findByTestId('pdf-overlay')
    await userEvent.click(screen.getByRole('button', { name: 'Polygon' }))
    fireEvent.mouseDown(overlay, { clientX: 10, clientY: 10 })
    blurFocus()
    await userEvent.keyboard('{Escape}')
    expect(screen.queryByTestId('pdf-polygon-draft')).toBeNull()

    await userEvent.click(screen.getByRole('button', { name: 'Point' }))
    fireEvent.mouseDown(overlay, { clientX: 42, clientY: 24 })
    expect((onChange.mock.calls.at(-1)?.[0] as AnnotationResult).shapes).toEqual([
      expect.objectContaining({ type: 'point', class: 'sign', page: 1, point: [42, 24] }),
    ])
  })

  it('zooms in steps and draws the page sharper when zoomed', async () => {
    const { doc } = setup()
    await screen.findByTestId('pdf-overlay')
    const page = await (doc.page as ReturnType<typeof vi.fn>).mock.results[0].value
    const render = page.render as ReturnType<typeof vi.fn>
    const firstScale = render.mock.calls.at(-1)?.[1] as number

    await userEvent.click(screen.getByRole('button', { name: 'Zoom in' }))
    expect(screen.getByTestId('pdf-zoom')).toHaveTextContent('150 %')
    await waitFor(() => expect(render.mock.calls.at(-1)?.[1]).toBeCloseTo(firstScale * 1.5))

    blurFocus()
    await userEvent.keyboard('0')
    expect(screen.getByTestId('pdf-zoom')).toHaveTextContent('100 %')
    await userEvent.click(screen.getByRole('button', { name: 'Zoom out' }))
    expect(screen.getByTestId('pdf-zoom')).toHaveTextContent('75 %')
  })
})

describe('PdfAnnotator spans and relations', () => {
  const SPAN_CLASSES: LabelClass[] = [
    ...CLASSES,
    { name: 'PER', display_name: 'Person', color: '#f59e0b', tools: ['span'], attributes: [] },
    { name: 'works', display_name: 'Works for', color: '#0ea5e9', tools: ['relation'], attributes: [] },
  ] as LabelClass[]

  // Page 1: "Total 42" (y 90-100), then two lines "Alice Smith" (y 190-200)
  // and "met Bob" (y 204-214). Page 2 has no text layer.
  function spanDocument(): PdfDocument {
    const run = (str: string, y: number, width: number) => ({
      str,
      transform: [10, 0, 0, 10, 100, y],
      width,
    })
    const words = wordBoxes(
      [run('Total 42', 700, 80), run('Alice Smith', 600, 110), run('met Bob', 586, 70)],
      [1, 0, 0, -1, 0, 800],
    )
    return {
      pageCount: 2,
      page: vi.fn(async (n: number) => ({
        width: 600,
        height: 800,
        render: vi.fn(async () => undefined),
        words: async () => (n === 1 ? words : []),
      })),
      destroy: vi.fn(async () => undefined),
    }
  }

  const bbox = {
    id: 'box',
    type: 'bbox' as const,
    class: 'total',
    attributes: {},
    confidence: null,
    page: 1,
    bbox: [95, 85, 185, 105] as [number, number, number, number],
  }
  const span = {
    id: 'sp',
    type: 'span' as const,
    class: 'PER',
    attributes: {},
    confidence: null,
    page: 1,
    boxes: [[100, 190, 210, 200]] as Array<[number, number, number, number]>,
    text: 'Alice Smith',
  }

  /** Keeps the value in state so multi-step flows see their own edits. */
  function Harness({
    initial,
    readOnly,
    onValue,
  }: {
    initial: AnnotationResult
    readOnly?: boolean
    onValue: (value: AnnotationResult) => void
  }): JSX.Element {
    const [value, setValue] = useState(initial)
    // Stable, or the component reopens the document on every edit.
    const [open] = useState(() => {
      const doc = spanDocument()
      return async () => doc
    })
    return (
      <PdfAnnotator
        pdfUrl="https://storage.example/doc.pdf"
        classes={SPAN_CLASSES}
        value={value}
        readOnly={readOnly}
        open={open}
        onChange={(next) => {
          setValue(next)
          onValue(next)
        }}
      />
    )
  }

  function renderHarness(shapes: AnnotationResult['shapes'] = [], readOnly = false) {
    const values: AnnotationResult[] = []
    render(
      <Harness
        initial={{ ...EMPTY, shapes }}
        readOnly={readOnly}
        onValue={(next) => values.push(next)}
      />,
    )
    return values
  }

  it('creates a span across two lines: one box per line and the words as text', async () => {
    const values = renderHarness()
    const overlay = await screen.findByTestId('pdf-overlay')
    await userEvent.click(screen.getByRole('button', { name: 'Span' }))

    drag(overlay, [180, 195], [150, 209])

    expect(values).toHaveLength(1)
    const created = values[0].shapes[0]
    expect(created).toMatchObject({
      type: 'span',
      class: 'PER',
      page: 1,
      text: 'Smith met Bob',
    })
    expect((created as { boxes: unknown }).boxes).toHaveLength(2)
    expect(created).not.toHaveProperty('start')
    expect(screen.getByTestId('pdf-selection-text')).toHaveTextContent('Smith met Bob')
    expect(screen.getByTestId(`pdf-span-${created.id}`)).toBeInTheDocument()
  })

  it('ignores a press that is not on a word', async () => {
    const values = renderHarness()
    const overlay = await screen.findByTestId('pdf-overlay')
    await userEvent.click(screen.getByRole('button', { name: 'Span' }))
    drag(overlay, [400, 400], [450, 450])
    expect(values).toHaveLength(0)
  })

  it('deletes a span and the relations that end on it', async () => {
    const relation = {
      id: 'rel',
      type: 'relation' as const,
      class: 'works',
      attributes: {},
      confidence: null,
      from: 'sp',
      to: 'box',
    }
    const values = renderHarness([bbox, span, relation])
    fireEvent.mouseDown(await screen.findByTestId('pdf-span-sp'))
    blurFocus()
    await userEvent.keyboard('{Delete}')
    expect(values.at(-1)?.shapes.map((s) => s.id)).toEqual(['box'])
  })

  it('links a span to a box with a relation of the chosen class', async () => {
    const values = renderHarness([bbox, span])
    fireEvent.mouseDown(await screen.findByTestId('pdf-span-sp'))
    await userEvent.click(screen.getByRole('button', { name: /Link/ }))
    expect(screen.getByRole('status')).toHaveTextContent('Linking from')

    fireEvent.mouseDown(screen.getByTestId('pdf-box-box'))

    const relation = values.at(-1)?.shapes.at(-1)
    expect(relation).toMatchObject({ type: 'relation', class: 'works', from: 'sp', to: 'box' })
    expect(relation).not.toHaveProperty('page')
    // The arrow is drawn and the relation is listed.
    expect(screen.getByTestId(`pdf-relation-${relation!.id}`)).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Relations' })).toHaveTextContent('Works for')
  })

  it('lists a relation between pages, without drawing it on either', async () => {
    const far = { ...bbox, id: 'far', page: 2 }
    renderHarness([span, far, { id: 'x', type: 'relation', class: 'works', attributes: {}, confidence: null, from: 'sp', to: 'far' }])
    await screen.findByTestId('pdf-overlay')
    expect(screen.queryByTestId('pdf-relation-x')).toBeNull()
    expect(screen.getByRole('region', { name: 'Relations' })).toHaveTextContent('page 2')
  })

  it('disables the span tool on a page without words', async () => {
    renderHarness()
    await screen.findByTestId('pdf-overlay')
    await userEvent.click(screen.getByRole('button', { name: 'Span' }))
    await userEvent.click(screen.getByRole('button', { name: 'Next page' }))
    expect(await screen.findByTestId('pdf-span-blocked')).toBeInTheDocument()
  })

  it('renders spans and relations read-only, with no editing controls', async () => {
    renderHarness(
      [
        bbox,
        span,
        { id: 'r', type: 'relation', class: 'works', attributes: {}, confidence: null, from: 'sp', to: 'box' },
      ],
      true,
    )
    expect(await screen.findByTestId('pdf-span-sp')).toBeInTheDocument()
    expect(screen.getByTestId('pdf-relation-r')).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Relations' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Span' })).toBeNull()
    expect(screen.queryByRole('button', { name: /Delete relation/ })).toBeNull()
  })
})
