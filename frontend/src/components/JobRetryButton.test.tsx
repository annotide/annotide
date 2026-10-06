import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { JobRetryButton } from './JobRetryButton'
import { ApiError, api } from '@/api/client'
import type { Job } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, api: { ...actual.api, retryJob: vi.fn() } }
})

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: 'j1',
    project_id: 'p1',
    type: 'export',
    status: 'failed',
    progress: 40,
    payload: { format: 'coco' },
    result: null,
    error: 'boom',
    attempts: 1,
    started_at: null,
    finished_at: null,
    created_at: '2026-09-28T10:00:00Z',
    updated_at: '2026-09-28T10:00:00Z',
    ...overrides,
  }
}

function renderButton(job: Job) {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
  const invalidate = vi.spyOn(client, 'invalidateQueries')
  render(
    <QueryClientProvider client={client}>
      <JobRetryButton job={job} />
    </QueryClientProvider>,
  )
  return { invalidate }
}

describe('JobRetryButton', () => {
  afterEach(() => vi.clearAllMocks())

  it.each(['queued', 'running', 'succeeded'] as const)('renders nothing for a %s job', (status) => {
    renderButton(makeJob({ status }))
    expect(screen.queryByRole('button')).toBeNull()
  })

  it('retries a failed job and refreshes its project job lists', async () => {
    vi.mocked(api.retryJob).mockResolvedValue(makeJob({ status: 'queued', error: null }))
    const { invalidate } = renderButton(makeJob())

    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))

    expect(api.retryJob).toHaveBeenCalledWith('j1')
    await waitFor(() =>
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ['projects', 'p1', 'jobs'] }),
    )
  })

  it('offers retry for a cancelled job too', () => {
    renderButton(makeJob({ status: 'cancelled', error: null }))
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('shows the server problem when the retry is refused', async () => {
    vi.mocked(api.retryJob).mockRejectedValue(
      new ApiError({
        type: 'about:blank',
        status: 409,
        title: 'Conflict',
        detail: 'A queued job cannot be retried.',
      }),
    )
    renderButton(makeJob())

    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('A queued job cannot be retried.')
  })
})
