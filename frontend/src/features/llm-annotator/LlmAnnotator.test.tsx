import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import type { AnnotationResult, LabelClass, LlmDocument, RankingShape, RatingShape } from '@/api/types'

import { LlmAnnotator } from './LlmAnnotator'

const RANKING_CLASS: LabelClass = {
  name: 'preference',
  display_name: 'Preference',
  color: '#3b82f6',
  tools: ['ranking'],
  attributes: [],
}

const RATING_CLASS: LabelClass = {
  name: 'helpfulness',
  display_name: 'Helpfulness',
  color: '#16a34a',
  tools: ['rating'],
  attributes: [],
  scale: { min: 1, max: 5, labels: { '1': 'Unusable', '5': 'Excellent' } },
}

const DOC_TWO_RESPONSES: LlmDocument = {
  messages: [{ role: 'user', content: 'How do I reset my password?' }],
  responses: [
    { id: 'a', content: 'Go to Settings → Security.' },
    { id: 'b', content: 'I cannot help with that.' },
  ],
}

const DOC_THREE_RESPONSES: LlmDocument = {
  messages: [],
  responses: [
    { id: 'a', content: 'Response A' },
    { id: 'b', content: 'Response B' },
    { id: 'c', content: 'Response C' },
  ],
}

const DOC_CONVERSATION_ONLY: LlmDocument = {
  messages: [
    { role: 'user', content: 'Hi' },
    { role: 'assistant', content: 'Hello, how can I help?' },
  ],
  responses: [],
}

const EMPTY_RESULT: AnnotationResult = {
  schema_version: 1,
  media_type: 'llm',
  classification: {},
  shapes: [],
}

function Harness({
  document,
  classes,
  initial,
  readOnly,
  onSelect,
}: {
  document: LlmDocument
  classes: LabelClass[]
  initial?: AnnotationResult
  readOnly?: boolean
  onSelect?: (id: string | null) => void
}): JSX.Element {
  const [value, setValue] = useState<AnnotationResult>(initial ?? EMPTY_RESULT)
  return (
    <>
      <LlmAnnotator
        document={document}
        classes={classes}
        value={value}
        onChange={setValue}
        readOnly={readOnly}
        onSelectionChange={onSelect}
      />
      <output data-testid="value">{JSON.stringify(value.shapes)}</output>
    </>
  )
}

function shapes(): Array<RankingShape | RatingShape> {
  return JSON.parse(screen.getByTestId('value').textContent ?? '[]') as Array<
    RankingShape | RatingShape
  >
}

describe('LlmAnnotator ranking', () => {
  it('labels a two-response ranking as a preference and writes the chosen order', async () => {
    render(<Harness document={DOC_TWO_RESPONSES} classes={[RANKING_CLASS]} />)
    expect(screen.getByText('B is better')).toBeInTheDocument()
    await userEvent.click(screen.getByText('A is better'))
    const [ranking] = shapes() as RankingShape[]
    expect(ranking).toMatchObject({ type: 'ranking', class: 'preference', order: ['a', 'b'] })
  })

  it('reorders three responses with move buttons, starting from document order', async () => {
    const onSelect = (id: string | null) => selected.push(id)
    const selected: Array<string | null> = []
    render(<Harness document={DOC_THREE_RESPONSES} classes={[RANKING_CLASS]} onSelect={onSelect} />)

    await userEvent.click(screen.getByRole('button', { name: 'Move Response B up' }))
    const [ranking] = shapes() as RankingShape[]
    expect(ranking.order).toEqual(['b', 'a', 'c'])
    expect(selected.at(-1)).toBe(ranking.id)
  })

  it('does not move the first entry further up', async () => {
    render(<Harness document={DOC_THREE_RESPONSES} classes={[RANKING_CLASS]} />)
    expect(screen.getByRole('button', { name: 'Move Response A up' })).toBeDisabled()
  })
})

describe('LlmAnnotator rating', () => {
  it('writes a rating with target response:<id> and value from the scale', async () => {
    render(<Harness document={DOC_THREE_RESPONSES} classes={[RATING_CLASS]} />)
    const responseAGroup = screen.getByRole('group', { name: 'Response A: Helpfulness' })
    await userEvent.click(within(responseAGroup).getByLabelText('Excellent'))
    const [rating] = shapes() as RatingShape[]
    expect(rating).toMatchObject({
      type: 'rating',
      class: 'helpfulness',
      target: 'response:a',
      value: 5,
    })
  })

  it('replaces an existing rating rather than adding a second one', async () => {
    const initial: AnnotationResult = {
      ...EMPTY_RESULT,
      shapes: [
        {
          id: '11111111-1111-4111-8111-111111111111',
          type: 'rating',
          class: 'helpfulness',
          attributes: {},
          confidence: null,
          target: 'response:a',
          value: 1,
        },
      ],
    }
    render(<Harness document={DOC_THREE_RESPONSES} classes={[RATING_CLASS]} initial={initial} />)
    const responseAGroup = screen.getByRole('group', { name: 'Response A: Helpfulness' })
    await userEvent.click(within(responseAGroup).getByLabelText('Excellent'))
    const result = shapes() as RatingShape[]
    expect(result).toHaveLength(1)
    expect(result[0]).toMatchObject({ target: 'response:a', value: 5 })
  })

  it('clearing a rating removes its shape', async () => {
    const initial: AnnotationResult = {
      ...EMPTY_RESULT,
      shapes: [
        {
          id: '11111111-1111-4111-8111-111111111111',
          type: 'rating',
          class: 'helpfulness',
          attributes: {},
          confidence: null,
          target: 'response:a',
          value: 1,
        },
      ],
    }
    render(<Harness document={DOC_THREE_RESPONSES} classes={[RATING_CLASS]} initial={initial} />)
    await userEvent.click(screen.getByLabelText('Clear Response A: Helpfulness rating'))
    expect(shapes()).toEqual([])
  })

  it('offers a per-turn rating for assistant messages and a conversation rating with no responses', () => {
    render(<Harness document={DOC_CONVERSATION_ONLY} classes={[RATING_CLASS]} />)
    expect(screen.getByRole('group', { name: 'Turn 1: Helpfulness' })).toBeInTheDocument()
    expect(screen.getByRole('group', { name: 'Whole conversation: Helpfulness' })).toBeInTheDocument()
  })

  it('read-only mode disables every control', () => {
    render(<Harness document={DOC_THREE_RESPONSES} classes={[RATING_CLASS]} readOnly />)
    for (const input of screen.getAllByRole('radio')) {
      expect(input).toBeDisabled()
    }
  })
})
