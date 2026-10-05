import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { BulkActionsBar, parseTags } from './BulkActionsBar'
import { ApiError, api } from '@/api/client'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      listMembers: vi.fn(),
      bulkItems: vi.fn(),
    },
  }
})

function renderBar(props: Partial<React.ComponentProps<typeof BulkActionsBar>> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onClear = vi.fn()
  const onApplied = vi.fn()
  render(
    <QueryClientProvider client={queryClient}>
      <BulkActionsBar
        projectId="p1"
        selectedIds={['i1', 'i2']}
        labels={new Map([['i2', 'images/dog.jpg']])}
        onClear={onClear}
        onApplied={onApplied}
        {...props}
      />
    </QueryClientProvider>,
  )
  return { onClear, onApplied }
}

describe('parseTags', () => {
  it('splits on commas and whitespace and de-duplicates', () => {
    expect(parseTags(' night, blurry  rain,night ')).toEqual(['night', 'blurry', 'rain'])
    expect(parseTags('')).toEqual([])
  })
})

describe('BulkActionsBar', () => {
  beforeEach(() => {
    vi.mocked(api.listMembers).mockResolvedValue([
      {
        user_id: 'u2',
        email: 'ann@example.com',
        display_name: 'Ann',
        role: 'annotator',
        created_at: '2024-01-01T00:00:00Z',
      },
    ])
  })

  afterEach(() => {
    vi.mocked(api.listMembers).mockReset()
    vi.mocked(api.bulkItems).mockReset()
  })

  it('assigns with only the fields the user set', async () => {
    vi.mocked(api.bulkItems).mockResolvedValue({ applied: 2, skipped: [] })
    const user = userEvent.setup()
    const { onApplied } = renderBar()

    await screen.findByRole('option', { name: 'Ann' })
    await user.selectOptions(screen.getByLabelText('Assignee'), 'u2')
    await user.type(screen.getByLabelText('Priority'), '4')
    await user.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() => {
      expect(api.bulkItems).toHaveBeenCalledWith('p1', {
        action: 'assign',
        item_ids: ['i1', 'i2'],
        type: 'annotate',
        assignee_id: 'u2',
        priority: 4,
      })
    })
    expect(await screen.findByRole('status')).toHaveTextContent('Applied to 2 items.')
    expect(onApplied).toHaveBeenCalledWith({ applied: 2, skipped: [] })
  })

  it('unassigns with assignee_id: null', async () => {
    vi.mocked(api.bulkItems).mockResolvedValue({ applied: 2, skipped: [] })
    const user = userEvent.setup()
    renderBar()

    await user.selectOptions(await screen.findByLabelText('Assignee'), 'none')
    await user.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() => {
      expect(api.bulkItems).toHaveBeenCalledWith('p1', {
        action: 'assign',
        item_ids: ['i1', 'i2'],
        type: 'annotate',
        assignee_id: null,
      })
    })
  })

  it('returns to queue and lists skipped reasons by item path', async () => {
    vi.mocked(api.bulkItems).mockResolvedValue({
      applied: 1,
      skipped: [{ item_id: 'i2', reason: 'no task in progress' }],
    })
    const user = userEvent.setup()
    renderBar()

    await user.selectOptions(screen.getByLabelText('Bulk action'), 'return')
    await user.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() => {
      expect(api.bulkItems).toHaveBeenCalledWith('p1', { action: 'return', item_ids: ['i1', 'i2'] })
    })
    expect(await screen.findByRole('status')).toHaveTextContent('Applied to 1 item, skipped 1.')
    await user.click(screen.getByRole('button', { name: 'Show reasons' }))
    expect(screen.getByText('images/dog.jpg')).toBeInTheDocument()
    expect(screen.getByText(/no task in progress/)).toBeInTheDocument()
  })

  it('approves with an optional comment', async () => {
    vi.mocked(api.bulkItems).mockResolvedValue({ applied: 2, skipped: [] })
    const user = userEvent.setup()
    renderBar()

    await user.selectOptions(screen.getByLabelText('Bulk action'), 'approve')
    await user.type(screen.getByLabelText('Review comment'), 'Looks good')
    await user.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() => {
      expect(api.bulkItems).toHaveBeenCalledWith('p1', {
        action: 'approve',
        item_ids: ['i1', 'i2'],
        comment: 'Looks good',
      })
    })
  })

  it('rejects only with a reason, sent as the comment', async () => {
    vi.mocked(api.bulkItems).mockResolvedValue({ applied: 2, skipped: [] })
    const user = userEvent.setup()
    renderBar()

    await user.selectOptions(screen.getByLabelText('Bulk action'), 'reject')
    await user.click(screen.getByRole('button', { name: 'Apply' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('A rejection needs a reason.')
    expect(api.bulkItems).not.toHaveBeenCalled()

    await user.type(screen.getByLabelText('Review comment'), 'Boxes too loose')
    await user.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() => {
      expect(api.bulkItems).toHaveBeenCalledWith('p1', {
        action: 'reject',
        item_ids: ['i1', 'i2'],
        comment: 'Boxes too loose',
      })
    })
  })

  it('refuses an empty tag request without calling the API', async () => {
    const user = userEvent.setup()
    renderBar()

    await user.selectOptions(screen.getByLabelText('Bulk action'), 'tag')
    await user.click(screen.getByRole('button', { name: 'Apply' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('at least one tag')
    expect(api.bulkItems).not.toHaveBeenCalled()
  })

  it('surfaces a server error and clears the selection on request', async () => {
    vi.mocked(api.bulkItems).mockRejectedValue(
      new ApiError({
        type: 'about:blank',
        status: 403,
        title: 'Forbidden',
        detail: 'Only a project owner or reviewer may run bulk operations.',
      }),
    )
    const user = userEvent.setup()
    const { onClear } = renderBar()

    await user.selectOptions(screen.getByLabelText('Bulk action'), 'return')
    await user.click(screen.getByRole('button', { name: 'Apply' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('owner or reviewer')

    await user.click(screen.getByRole('button', { name: 'Clear selection' }))
    expect(onClear).toHaveBeenCalled()
  })
})
