import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  formatBones,
  parseBones,
  renameSkeletonPoints,
  SchemaEditor,
  validateSchema,
} from './SchemaEditor'
import { api } from '@/api/client'
import type { LabelSchemaVersion } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      listSchemas: vi.fn(),
      createSchemaVersion: vi.fn(),
    },
  }
})

function makeVersion(overrides: Partial<LabelSchemaVersion> = {}): LabelSchemaVersion {
  return {
    id: 'v1',
    label_schema_id: 's1',
    version: 1,
    definition: {
      version: 1,
      classes: [
        {
          name: 'cat',
          display_name: 'Cat',
          color: '#ff0000',
          hotkey: 'c',
          tools: ['bbox'],
          attributes: [],
        },
      ],
      classification: [],
    },
    created_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderEditor(readOnly = false) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <SchemaEditor projectId="p1" readOnly={readOnly} />
    </QueryClientProvider>,
  )
}

describe('validateSchema', () => {
  it('rejects duplicate class names', () => {
    const errors = validateSchema(
      [
        { name: 'cat', display_name: 'Cat', color: '#fff', tools: ['bbox'], attributes: [] },
        { name: 'cat', display_name: 'Cat 2', color: '#000', tools: ['bbox'], attributes: [] },
      ],
      [],
    )
    expect(errors.some((e) => e.includes('used more than once'))).toBe(true)
  })

  it('rejects a class with no tools', () => {
    const errors = validateSchema(
      [{ name: 'cat', display_name: 'Cat', color: '#fff', tools: [], attributes: [] }],
      [],
    )
    expect(errors.some((e) => e.includes('at least one tool'))).toBe(true)
  })

  it('rejects a select attribute with no options', () => {
    const errors = validateSchema(
      [
        {
          name: 'cat',
          display_name: 'Cat',
          color: '#fff',
          tools: ['bbox'],
          attributes: [{ name: 'size', type: 'select', required: false, options: undefined }],
        },
      ],
      [],
    )
    expect(errors.some((e) => e.includes('needs at least one option'))).toBe(true)
  })

  it('accepts a valid schema', () => {
    const errors = validateSchema(
      [{ name: 'cat', display_name: 'Cat', color: '#fff', hotkey: 'c', tools: ['bbox'], attributes: [] }],
      [],
    )
    expect(errors).toEqual([])
  })
})

describe('SchemaEditor', () => {
  afterEach(() => {
    vi.mocked(api.listSchemas).mockReset()
    vi.mocked(api.createSchemaVersion).mockReset()
  })

  it('does not call the API when the schema is invalid (duplicate names, missing tools)', async () => {
    vi.mocked(api.listSchemas).mockResolvedValue([])
    const user = userEvent.setup()

    renderEditor()

    await user.click(await screen.findByRole('button', { name: 'Add class' }))
    await user.click(screen.getByRole('button', { name: 'Add class' }))

    await user.type(screen.getByLabelText('class-name-0'), 'cat')
    await user.type(screen.getByLabelText('class-name-1'), 'cat')
    // Give the first class a tool but leave the second without one.
    await user.click(screen.getAllByRole('checkbox', { name: 'bbox' })[0])

    await user.click(screen.getByRole('button', { name: /Save as version/ }))

    expect(api.createSchemaVersion).not.toHaveBeenCalled()
    expect(await screen.findByRole('alert')).toBeInTheDocument()
  })

  it('submits version max+1 with classes and classification when valid', async () => {
    vi.mocked(api.listSchemas).mockResolvedValue([makeVersion({ version: 3 })])
    vi.mocked(api.createSchemaVersion).mockResolvedValue(makeVersion({ version: 4 }))
    const user = userEvent.setup()

    renderEditor()

    await screen.findByText((content) => content.startsWith('v3'))
    await user.click(screen.getByRole('button', { name: /Save as version 4/ }))

    await waitFor(() => {
      expect(api.createSchemaVersion).toHaveBeenCalledWith('p1', {
        version: 4,
        classes: [
          {
            name: 'cat',
            display_name: 'Cat',
            color: '#ff0000',
            hotkey: 'c',
            tools: ['bbox'],
            attributes: [],
          },
        ],
        classification: [],
      })
    })
  })

  it('hides write controls when read-only', async () => {
    vi.mocked(api.listSchemas).mockResolvedValue([makeVersion()])

    renderEditor(true)

    await screen.findByLabelText('class-name-0')
    expect(screen.queryByRole('button', { name: 'Add class' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Save as version/ })).not.toBeInTheDocument()
    expect(screen.getByLabelText('class-name-0')).toBeDisabled()
  })
})

describe('skeleton editing', () => {
  it('parses bones by name, including names with dashes', () => {
    const points = ['head', 'left-hand', 'right_hand']
    expect(parseBones('head-left-hand, head - right_hand, head-left-hand', points)).toEqual({
      edges: [
        [0, 1],
        [0, 2],
      ],
      unknown: [],
    })
    expect(parseBones('head-tail, head-head', points).unknown).toEqual(['head-tail', 'head-head'])
    expect(formatBones({ points, edges: [[0, 2]] })).toBe('head-right_hand')
  })

  it('keeps bones across a rename by name and drops those that lost an end', () => {
    const skeleton = { points: ['a', 'b', 'c'], edges: [[0, 1], [1, 2]] as [number, number][] }
    expect(renameSkeletonPoints(skeleton, ['c', 'b', 'd'])).toEqual({
      points: ['c', 'b', 'd'],
      edges: [[1, 0]],
    })
  })

  it('requires keypoint names for the keypoints tool', () => {
    const cls = { name: 'p', display_name: 'P', color: '#000000', attributes: [] }
    expect(
      validateSchema([{ ...cls, tools: ['keypoints'], skeleton: { points: [], edges: [] } }], []),
    ).toContain('Class "p" needs at least one keypoint name.')
    expect(
      validateSchema(
        [{ ...cls, tools: ['keypoints'], skeleton: { points: ['a', 'a'], edges: [] } }],
        [],
      ),
    ).toContain('Class "p" has a keypoint name more than once.')
  })

  it('saves a skeleton edited in the form', async () => {
    vi.mocked(api.listSchemas).mockResolvedValue([makeVersion()])
    vi.mocked(api.createSchemaVersion).mockResolvedValue(makeVersion({ version: 2 }))
    const user = userEvent.setup()
    renderEditor()

    await screen.findByText((content) => content.startsWith('v1'))
    await user.click(screen.getByRole('checkbox', { name: 'keypoints' }))
    await user.type(screen.getByLabelText('class-0-keypoints'), 'head, tail')
    await user.tab()
    await user.type(screen.getByLabelText('class-0-bones'), 'head-nose')
    await user.tab()
    expect(screen.getByRole('alert')).toHaveTextContent('Unknown bones: head-nose')
    await user.click(screen.getByRole('button', { name: /Save as version 2/ }))
    expect(api.createSchemaVersion).not.toHaveBeenCalled()

    await user.clear(screen.getByLabelText('class-0-bones'))
    await user.type(screen.getByLabelText('class-0-bones'), 'head-tail')
    await user.tab()
    await user.click(screen.getByRole('button', { name: /Save as version 2/ }))
    await waitFor(() => {
      expect(api.createSchemaVersion).toHaveBeenCalledWith('p1', {
        version: 2,
        classes: [
          expect.objectContaining({
            tools: ['bbox', 'keypoints'],
            skeleton: { points: ['head', 'tail'], edges: [[0, 1]] },
          }),
        ],
        classification: [],
      })
    })
  })

  it('requires a sane scale for the rating tool', () => {
    const cls = { name: 'h', display_name: 'H', color: '#000000', attributes: [] }
    expect(validateSchema([{ ...cls, tools: ['rating'] }], [])).toContain(
      'Class "h" needs a rating scale.',
    )
    expect(
      validateSchema([{ ...cls, tools: ['rating'], scale: { min: 5, max: 5 } }], []),
    ).toContain('The scale of "h" needs its minimum below its maximum.')
    expect(
      validateSchema([{ ...cls, tools: ['rating'], scale: { min: 0, max: 30 } }], []),
    ).toContain('The scale of "h" has more than 21 steps.')
  })

  it('saves a rating scale edited in the form, and drops it with the tool', async () => {
    vi.mocked(api.listSchemas).mockResolvedValue([makeVersion()])
    vi.mocked(api.createSchemaVersion).mockResolvedValue(makeVersion({ version: 2 }))
    const user = userEvent.setup()
    renderEditor()

    await screen.findByText((content) => content.startsWith('v1'))
    await user.click(screen.getByRole('checkbox', { name: 'rating' }))
    await user.clear(screen.getByLabelText('class-0-scale-max'))
    await user.type(screen.getByLabelText('class-0-scale-max'), '7')
    await user.type(screen.getByLabelText('class-0-scale-max-label'), 'Excellent')
    await user.click(screen.getByRole('button', { name: /Save as version 2/ }))

    await waitFor(() => {
      expect(api.createSchemaVersion).toHaveBeenCalledWith('p1', {
        version: 2,
        classes: [
          expect.objectContaining({
            tools: ['bbox', 'rating'],
            scale: { min: 1, max: 7, labels: { '7': 'Excellent' } },
          }),
        ],
        classification: [],
      })
    })

    await user.click(screen.getByRole('checkbox', { name: 'rating' }))
    expect(screen.queryByLabelText('class-0-scale-max')).not.toBeInTheDocument()
  })
})
