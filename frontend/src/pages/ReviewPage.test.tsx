import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ReviewPage } from './ReviewPage'
import { api } from '@/api/client'
import { useTaskStore } from '@/lib/store'
import type { Annotation, ConsensusView, Item, LabelSchemaVersion, Task } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      nextTask: vi.fn(),
      releaseTask: vi.fn(),
      extendTask: vi.fn(),
      getItem: vi.fn(),
      listAnnotations: vi.fn(),
      getProject: vi.fn(() => new Promise(() => {})),
      listSchemas: vi.fn(),
      reviewAnnotation: vi.fn(),
      listComments: vi.fn().mockResolvedValue([]),
      createComment: vi.fn(),
      resolveComment: vi.fn(),
      getConsensus: vi.fn(),
      resolveConsensus: vi.fn(),
      setGold: vi.fn(),
      clearGold: vi.fn(),
    },
  }
})

vi.mock('@/features/annotator', () => ({
  ImageAnnotator: () => <div data-testid="annotator" />,
}))

function makeItem(overrides: Partial<Item> = {}): Item {
  return {
    id: 'i1',
    project_id: 'p1',
    connector_id: 'c1',
    path: 'raw/image-1.jpg',
    media_type: 'image',
    etag: null,
    size_bytes: 1024,
    width: 800,
    height: 600,
    meta: {},
    status: 'submitted',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    media_url: 'https://example.com/image-1.jpg',
    ...overrides,
  }
}

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 't1',
    item_id: 'i1',
    project_id: 'p1',
    type: 'review',
    assignee_id: null,
    status: 'in_progress',
    locked_by_id: 'u1',
    locked_until: '2024-01-01T01:00:00Z',
    priority: 0,
    deadline: null,
    slot: null,
    region: null,
    gold: false,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeAnnotation(overrides: Partial<Annotation> = {}): Annotation {
  return {
    id: 'a1',
    item_id: 'i1',
    task_id: 't1',
    version: 1,
    author_user_id: 'u1',
    author_model_version_id: null,
    source: 'human',
    label_schema_version_id: 's1',
    result: { schema_version: 1, media_type: 'image', classification: {}, shapes: [] },
    status: 'submitted',
    duration_ms: null,
    blob_path: null,
    kind: 'primary',
    created_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeSchemaPage(): LabelSchemaVersion[] {
  return [
    {
      id: 's1',
      label_schema_id: 'ls1',
      version: 1,
      definition: { version: 1, classes: [], classification: [] },
      created_at: '2024-01-01T00:00:00Z',
    },
  ]
}

function renderPage(initialEntry: string) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <Routes>
          <Route path="/projects/:projectId/review/:itemId?" element={<ReviewPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function mockItemLoaded(overrides: Partial<Item> = {}, annotations: Annotation[] = [makeAnnotation()]) {
  vi.mocked(api.getItem).mockResolvedValue(makeItem(overrides))
  vi.mocked(api.listAnnotations).mockResolvedValue(annotations)
  vi.mocked(api.listSchemas).mockResolvedValue(makeSchemaPage())
}

describe('ReviewPage', () => {
  afterEach(() => {
    useTaskStore.getState().clearTask()
    vi.mocked(api.nextTask).mockReset()
    vi.mocked(api.releaseTask).mockReset()
    vi.mocked(api.extendTask).mockReset()
    vi.mocked(api.getItem).mockReset()
    vi.mocked(api.listAnnotations).mockReset()
    vi.mocked(api.listSchemas).mockReset()
    vi.mocked(api.reviewAnnotation).mockReset()
  })

  it('claims the next review task and renders the claimed item', async () => {
    vi.mocked(api.nextTask).mockResolvedValue(makeTask())
    mockItemLoaded()

    renderPage('/projects/p1/review')

    await waitFor(() => {
      expect(api.nextTask).toHaveBeenCalledWith({ project_id: 'p1', type: 'review' })
    })

    await waitFor(() => {
      expect(screen.getByText('raw/image-1.jpg')).toBeInTheDocument()
    })
  })

  it('shows an empty state when the queue has no open tasks, and can retry', async () => {
    vi.mocked(api.nextTask).mockResolvedValue(null)
    vi.mocked(api.listSchemas).mockResolvedValue(makeSchemaPage())

    renderPage('/projects/p1/review')

    await waitFor(() => {
      expect(screen.getByText('Nothing to review')).toBeInTheDocument()
    })
    expect(api.nextTask).toHaveBeenCalledTimes(1)

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Check again' }))

    await waitFor(() => {
      expect(api.nextTask).toHaveBeenCalledTimes(2)
    })
  })

  it('approves the newest submitted annotation version', async () => {
    const submitted = makeAnnotation({ id: 'a2', version: 2, status: 'submitted' })
    const draft = makeAnnotation({ id: 'a1', version: 1, status: 'draft' })
    mockItemLoaded({}, [submitted, draft])
    vi.mocked(api.reviewAnnotation).mockResolvedValue({ ...submitted, status: 'approved' })

    renderPage('/projects/p1/review/i1')

    const user = userEvent.setup()
    const approveButton = await screen.findByRole('button', { name: 'Approve' })
    await user.click(approveButton)

    await waitFor(() => {
      expect(api.reviewAnnotation).toHaveBeenCalledWith('a2', {
        approve: true,
        comment: undefined,
      })
    })
  })

  it('disables Reject until a comment is entered, then rejects with it', async () => {
    const submitted = makeAnnotation({ id: 'a2', version: 2, status: 'submitted' })
    mockItemLoaded({}, [submitted])
    vi.mocked(api.reviewAnnotation).mockResolvedValue({ ...submitted, status: 'rejected' })

    renderPage('/projects/p1/review/i1')

    const rejectButton = await screen.findByRole('button', { name: 'Reject' })
    expect(rejectButton).toBeDisabled()

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Comment'), 'nope')
    expect(rejectButton).toBeEnabled()

    await user.click(rejectButton)

    await waitFor(() => {
      expect(api.reviewAnnotation).toHaveBeenCalledWith('a2', {
        approve: false,
        comment: 'nope',
      })
    })
  })

  it('resolves a consensus item by fusing, not by reviewing a consensus version', async () => {
    const anna = makeAnnotation({ id: 'c1', version: 1, kind: 'consensus', status: 'submitted' })
    const ben = makeAnnotation({ id: 'c2', version: 2, kind: 'consensus', status: 'submitted' })
    mockItemLoaded({}, [ben, anna])
    const view: ConsensusView = {
      expected: 2,
      annotators: [
        {
          user_id: 'u1',
          email: 'anna@example.com',
          display_name: 'Anna',
          annotation_id: 'c1',
          version: 1,
          status: 'submitted',
          created_at: '2024-01-01T00:00:00Z',
        },
        {
          user_id: 'u2',
          email: 'ben@example.com',
          display_name: 'Ben',
          annotation_id: 'c2',
          version: 2,
          status: 'submitted',
          created_at: '2024-01-01T00:00:00Z',
        },
      ],
      agreement: {
        annotators: [],
        classification: [],
        shapes: { mean_iou: 0.9, f1: 1, iou_threshold: 0.5, envelope_iou: false },
        spans: { f1_exact: null, f1_overlap: null },
        pairs: [],
      },
      preview: { schema_version: 1, media_type: 'image', classification: {}, shapes: [] },
      conflicts: ['classification.weather'],
    }
    vi.mocked(api.getConsensus).mockResolvedValue(view)
    vi.mocked(api.resolveConsensus).mockResolvedValue({ ...anna, id: 'p1', kind: 'primary' })

    renderPage('/projects/p1/review/i1')

    expect(await screen.findByText('Consensus (2/2)')).toBeInTheDocument()
    expect(screen.getByText('classification.weather')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()

    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Fuse and approve' }))
    await waitFor(() =>
      expect(api.resolveConsensus).toHaveBeenCalledWith('i1', {
        method: 'fuse',
        comment: undefined,
      }),
    )
    expect(api.reviewAnnotation).not.toHaveBeenCalled()
  })

  it('offers an approved item as a gold reference', async () => {
    const approved = makeAnnotation({ id: 'a3', version: 3, status: 'approved' })
    mockItemLoaded({ status: 'approved' }, [approved])
    vi.mocked(api.setGold).mockResolvedValue(makeItem({ meta: { gold_annotation_id: 'a3' } }))

    renderPage('/projects/p1/review/i1')

    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Use as gold reference' }))
    await waitFor(() => expect(api.setGold).toHaveBeenCalledWith('i1', 'a3'))
    expect(await screen.findByRole('button', { name: 'Remove gold reference' })).toBeInTheDocument()
  })

  it('shows a submitted video item in the read-only video annotator', async () => {
    const submitted = makeAnnotation({
      result: {
        schema_version: 1,
        media_type: 'video',
        classification: {},
        shapes: [
          {
            id: 's1',
            type: 'bbox',
            class: 'car',
            attributes: {},
            confidence: null,
            frame: 0,
            track_id: 't1',
            keyframe: true,
            outside: false,
            bbox: [1, 1, 2, 2],
          },
        ],
      },
    })
    mockItemLoaded(
      { media_type: 'video', path: 'clips/a.mp4', meta: { fps: 30 } },
      [submitted],
    )

    renderPage('/projects/p1/review/i1')

    await waitFor(() => expect(screen.getByTestId('video-overlay')).toBeInTheDocument())
    expect(screen.queryByTestId('annotator')).not.toBeInTheDocument()
    // Read-only: no class picker or editing controls.
    expect(screen.queryByText('Class')).not.toBeInTheDocument()
  })
})
