import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CommentsPanel } from './CommentsPanel'
import { api } from '@/api/client'
import type { Comment } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      listComments: vi.fn(),
      createComment: vi.fn(),
      resolveComment: vi.fn(),
    },
  }
})

function makeComment(overrides: Partial<Comment> = {}): Comment {
  return {
    id: 'c1',
    project_id: 'p1',
    item_id: 'i1',
    annotation_id: null,
    parent_id: null,
    author_id: 'aaaaaaaa-0000-0000-0000-000000000000',
    body: 'Is this a car?',
    anchor: null,
    resolved_at: null,
    created_at: new Date(Date.now() - 60_000).toISOString(),
    updated_at: new Date(Date.now() - 60_000).toISOString(),
    ...overrides,
  }
}

function renderPanel(annotationId?: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <CommentsPanel itemId="i1" annotationId={annotationId} />
    </QueryClientProvider>,
  )
}

describe('CommentsPanel', () => {
  beforeEach(() => {
    vi.mocked(api.createComment).mockResolvedValue(makeComment({ id: 'new' }))
    vi.mocked(api.resolveComment).mockResolvedValue(makeComment({ resolved_at: 'x' }))
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('renders the thread with replies nested under their parent', async () => {
    vi.mocked(api.listComments).mockResolvedValue([
      makeComment(),
      makeComment({ id: 'c2', parent_id: 'c1', body: 'Yes, a car.' }),
      makeComment({ id: 'c3', body: 'Second thread', resolved_at: '2026-09-18T09:00:00Z' }),
    ])

    renderPanel()

    await waitFor(() => expect(screen.getByText('Is this a car?')).toBeInTheDocument())
    const lists = screen.getAllByRole('list')
    // Outer list has two top-level items; the reply lives in a nested list.
    const [outer] = lists
    expect(within(outer).getAllByRole('listitem')).toHaveLength(3) // 2 top-level + 1 nested
    expect(screen.getByText('Yes, a car.')).toBeInTheDocument()
    expect(screen.getByText('resolved')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reopen' })).toBeInTheDocument()
  })

  it('posts a new top-level comment scoped to the annotation', async () => {
    vi.mocked(api.listComments).mockResolvedValue([])
    const user = userEvent.setup()

    renderPanel('a1')
    await waitFor(() => expect(screen.getByText('No comments yet.')).toBeInTheDocument())

    const post = screen.getByRole('button', { name: 'Post' })
    expect(post).toBeDisabled()
    await user.type(screen.getByLabelText('New comment'), 'Looks blurry')
    await user.click(post)

    await waitFor(() =>
      expect(api.createComment).toHaveBeenCalledWith('i1', {
        body: 'Looks blurry',
        annotation_id: 'a1',
        parent_id: undefined,
      }),
    )
  })

  it('replies with parent_id after pressing Reply', async () => {
    vi.mocked(api.listComments).mockResolvedValue([makeComment()])
    const user = userEvent.setup()

    renderPanel()
    await waitFor(() => expect(screen.getByText('Is this a car?')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Reply' }))
    expect(screen.getByText(/Replying to/)).toBeInTheDocument()
    await user.type(screen.getByLabelText('New comment'), 'Yes')
    await user.click(screen.getByRole('button', { name: 'Post' }))

    await waitFor(() =>
      expect(api.createComment).toHaveBeenCalledWith('i1', {
        body: 'Yes',
        annotation_id: undefined,
        parent_id: 'c1',
      }),
    )
  })

  it('resolves and reopens through the toggle', async () => {
    vi.mocked(api.listComments).mockResolvedValue([makeComment()])
    const user = userEvent.setup()

    renderPanel()
    await waitFor(() => expect(screen.getByText('Is this a car?')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Resolve' }))
    await waitFor(() => expect(api.resolveComment).toHaveBeenCalledWith('c1', true))
  })
})
