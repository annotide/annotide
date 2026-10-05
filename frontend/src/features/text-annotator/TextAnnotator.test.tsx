import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'

import type { AnnotationResult, LabelClass, RelationShape, TextSpanShape } from '@/api/types'

import { TextAnnotator } from './TextAnnotator'

const CLASSES: LabelClass[] = [
  {
    name: 'PER',
    display_name: 'Person',
    color: '#e11d48',
    hotkey: '1',
    tools: ['span'],
    attributes: [],
  },
  {
    name: 'ORG',
    display_name: 'Org',
    color: '#2563eb',
    hotkey: '2',
    tools: ['span'],
    attributes: [],
  },
  {
    name: 'works_for',
    display_name: 'works for',
    color: '#16a34a',
    tools: ['relation'],
    attributes: [],
  },
]

const TEXT = 'Alice 😀 works at Contoso.'

function Harness({
  initial,
  text = TEXT,
  pages,
  onPageFocus,
}: {
  initial?: AnnotationResult
  text?: string
  pages?: number[][]
  onPageFocus?: (page: number) => void
}): JSX.Element {
  const [value, setValue] = useState<AnnotationResult>(
    initial ?? {
      schema_version: 1,
      media_type: 'text',
      classification: {},
      shapes: [],
    },
  )
  return (
    <>
      <TextAnnotator
        text={text}
        classes={CLASSES}
        value={value}
        onChange={setValue}
        pages={pages}
        onPageFocus={onPageFocus}
      />
      <output data-testid="value">{JSON.stringify(value.shapes)}</output>
    </>
  )
}

function shapes(): Array<TextSpanShape | RelationShape> {
  return JSON.parse(screen.getByTestId('value').textContent ?? '[]') as Array<
    TextSpanShape | RelationShape
  >
}

/** Select UTF-16 `[from, to)` of the body's single text node and release the mouse. */
function selectText(from: number, to: number): void {
  const body = screen.getByTestId('text-body')
  const node = body.firstChild?.firstChild
  if (!node) throw new Error('no text node')
  const range = document.createRange()
  range.setStart(node, from)
  range.setEnd(node, to)
  const selection = window.getSelection()
  selection?.removeAllRanges()
  selection?.addRange(range)
  fireEvent.mouseUp(body)
}

function span(id: string, start: number, end: number, cls: string): TextSpanShape {
  return {
    id,
    type: 'span',
    class: cls,
    attributes: {},
    confidence: null,
    start,
    end,
  }
}

describe('TextAnnotator', () => {
  it('creates a span in code points, trimmed, with the active class', async () => {
    render(<Harness />)
    // "works" starts at UTF-16 offset 9 (the emoji is two units) but code point 8.
    await userEvent.click(screen.getByRole('button', { name: /Org/ }))
    selectText(8, 15)
    const [created] = shapes() as TextSpanShape[]
    expect(created).toMatchObject({
      type: 'span',
      class: 'ORG',
      start: 8,
      end: 13,
      text: 'works',
    })
  })

  it('links two spans with a relation and deletes a span with its relations', async () => {
    const initial: AnnotationResult = {
      schema_version: 1,
      media_type: 'text',
      classification: {},
      shapes: [
        span('11111111-1111-4111-8111-111111111111', 0, 5, 'PER'),
        span('22222222-2222-4222-8222-222222222222', 17, 24, 'ORG'),
      ],
    }
    render(<Harness initial={initial} />)

    await userEvent.click(screen.getByText('Alice'))
    await userEvent.click(screen.getByRole('button', { name: /Link/ }))
    expect(screen.getByText(/Linking from Person “Alice”/)).toBeInTheDocument()
    await userEvent.click(screen.getByText('Contoso'))

    const relation = shapes().find((s) => s.type === 'relation') as RelationShape
    expect(relation).toMatchObject({
      class: 'works_for',
      from: '11111111-1111-4111-8111-111111111111',
      to: '22222222-2222-4222-8222-222222222222',
    })
    expect(screen.getByRole('region', { name: 'Relations' })).toHaveTextContent(
      'Person “Alice” —works for→ Org “Contoso”',
    )

    await userEvent.click(screen.getByText('Alice'))
    await userEvent.keyboard('{Delete}')
    expect(shapes().map((s) => s.id)).toEqual(['22222222-2222-4222-8222-222222222222'])
  })

  it('renders overlapping spans as separate runs covered by both', () => {
    const initial: AnnotationResult = {
      schema_version: 1,
      media_type: 'text',
      classification: {},
      shapes: [span('a', 0, 5, 'PER'), span('b', 2, 13, 'ORG')],
    }
    render(<Harness initial={initial} />)
    const both = screen.getByText('ice')
    expect(both.getAttribute('data-span-ids')).toBe('b a')
  })

  describe('page markers (PDF text mode)', () => {
    const PDF_TEXT = 'Cover page\n\nInvoice total'

    it('marks where each page begins without shifting offsets', async () => {
      render(<Harness text={PDF_TEXT} pages={[[0, 10], [12, 25]]} />)
      const markers = screen.getAllByTestId('page-marker')
      expect(markers.map((m) => m.dataset.label)).toEqual(['Page 1', 'Page 2'])
      // The label is CSS content: the body's text is still exactly the document.
      expect(screen.getByTestId('text-body').textContent).toBe(PDF_TEXT)

      await userEvent.click(screen.getByRole('button', { name: /Person/ }))
      const invoice = screen.getByText('Invoice total').firstChild
      if (!invoice) throw new Error('no text node')
      const range = document.createRange()
      range.setStart(invoice, 0)
      range.setEnd(invoice, 7)
      window.getSelection()?.removeAllRanges()
      window.getSelection()?.addRange(range)
      fireEvent.mouseUp(screen.getByTestId('text-body'))

      expect(shapes()[0]).toMatchObject({ start: 12, end: 19, text: 'Invoice' })
    })

    it('asks for the page of a clicked marker or a selected span', async () => {
      const onPageFocus = vi.fn()
      const initial: AnnotationResult = {
        schema_version: 1,
        media_type: 'text',
        classification: {},
        shapes: [span('33333333-3333-4333-8333-333333333333', 12, 19, 'PER')],
      }
      render(
        <Harness text={PDF_TEXT} pages={[[0, 10], [12, 25]]} initial={initial} onPageFocus={onPageFocus} />,
      )

      await userEvent.click(screen.getByRole('button', { name: 'Show page 1 of the PDF' }))
      expect(onPageFocus).toHaveBeenLastCalledWith(1)
      await userEvent.click(screen.getByText('Invoice'))
      expect(onPageFocus).toHaveBeenLastCalledWith(2)
    })

    it('leaves markers inert without a PDF to turn', () => {
      render(<Harness text={PDF_TEXT} pages={[[0, 10], [12, 25]]} />)
      for (const marker of screen.getAllByTestId('page-marker')) expect(marker).toBeDisabled()
    })

    it('labels pages without text and shows nothing for a single page', () => {
      const { unmount } = render(<Harness text={PDF_TEXT} pages={[[0, 10], [10, 10], [12, 25]]} />)
      expect(screen.getAllByTestId('page-marker').map((m) => m.dataset.label)).toEqual([
        'Page 1',
        'Page 2 (no text)',
        'Page 3',
      ])
      unmount()
      render(<Harness text="One page" pages={[[0, 8]]} />)
      expect(screen.queryByTestId('page-marker')).not.toBeInTheDocument()
    })
  })
})
