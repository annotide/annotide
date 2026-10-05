import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ScanPanel } from './ScanPanel'
import { api } from '@/api/client'
import type { Job } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, api: { ...actual.api, scanProject: vi.fn(), getJob: vi.fn() } }
})

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: 'j1',
    project_id: 'p1',
    type: 'scan_source',
    status: 'queued',
    progress: 0,
    payload: {},
    result: null,
    error: null,
    attempts: 0,
    started_at: null,
    finished_at: null,
    created_at: '2026-10-02T10:00:00Z',
    updated_at: '2026-10-02T10:00:00Z',
    ...overrides,
  }
}

function renderPanel(sourceConnectorId: string | null) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ScanPanel projectId="p1" sourceConnectorId={sourceConnectorId} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ScanPanel', () => {
  afterEach(() => {
    vi.mocked(api.scanProject).mockReset()
    vi.mocked(api.getJob).mockReset()
  })

  it('points at Settings when the project has no source connector', () => {
    renderPanel(null)

    expect(screen.queryByRole('button', { name: 'Scan now' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Choose one in Settings' })).toHaveAttribute(
      'href',
      '/projects/p1/settings',
    )
  })

  it('queues a scan and reports how many items it created', async () => {
    vi.mocked(api.scanProject).mockResolvedValue(makeJob())
    vi.mocked(api.getJob).mockResolvedValue(
      makeJob({ status: 'succeeded', result: { items_created: 3, items_updated: 0 } }),
    )
    const user = userEvent.setup()
    renderPanel('c1')

    await user.click(screen.getByRole('button', { name: 'Scan now' }))

    expect(api.scanProject).toHaveBeenCalledWith('p1')
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('Scan finished: 3 new items.'),
    )
    expect(screen.getByRole('button', { name: 'Scan now' })).toBeEnabled()
  })

  it('shows the job error when the scan fails', async () => {
    vi.mocked(api.scanProject).mockResolvedValue(makeJob())
    vi.mocked(api.getJob).mockResolvedValue(
      makeJob({ status: 'failed', error: 'container not found' }),
    )
    const user = userEvent.setup()
    renderPanel('c1')

    await user.click(screen.getByRole('button', { name: 'Scan now' }))

    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('container not found'),
    )
  })
})
