import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DashboardPage } from './DashboardPage'
import { api } from '@/api/client'
import type { Project, ProjectStats } from '@/api/types'
import { DEFAULT_WORKFLOW } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      getProject: vi.fn(),
      getProjectStats: vi.fn(),
    },
  }
})

const project: Project = {
  id: 'p1',
  organization_id: 'org1',
  name: 'Traffic',
  description: null,
  label_schema_id: null,
  source_connector_id: null,
  result_connector_id: null,
  cache_connector_id: null,
  source_prefix: null,
  source_glob: null,
  settings: {},
  workflow: DEFAULT_WORKFLOW,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
}

function makeStats(overrides: Partial<ProjectStats> = {}): ProjectStats {
  return {
    items: {
      total: 8,
      by_status: {
        new: 2,
        prelabeled: 0,
        annotating: 1,
        submitted: 1,
        in_review: 0,
        approved: 3,
        rejected: 1,
        skipped: 0,
      },
    },
    tasks: {
      annotate: { open: 2, in_progress: 1, done: 4, cancelled: 0 },
      review: { open: 1, in_progress: 0, done: 4, cancelled: 0 },
    },
    annotations: {
      versions: 9,
      by_source: { human: 8, model: 1 },
      latest_by_status: { draft: 1, submitted: 1, approved: 3, rejected: 1 },
    },
    review: { approved: 3, rejected: 1, rejection_rate: 0.25 },
    throughput: [
      { day: '2024-01-01', submitted: 0, approved: 2, rejected: 0 },
      { day: '2024-01-02', submitted: 1, approved: 1, rejected: 1 },
    ],
    classes: [
      { label: 'car', count: 12 },
      { label: 'bus', count: 3 },
    ],
    annotators: [
      {
        user_id: 'u1',
        display_name: 'Alice',
        submitted: 1,
        approved: 2,
        rejected: 0,
      },
      {
        user_id: 'u2',
        display_name: 'Bob',
        submitted: 0,
        approved: 1,
        rejected: 1,
      },
    ],
    ...overrides,
  }
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/projects/p1/dashboard']}>
        <Routes>
          <Route path="/projects/:projectId/dashboard" element={<DashboardPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('DashboardPage', () => {
  beforeEach(() => {
    vi.mocked(api.getProject).mockResolvedValue(project)
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('renders every panel from the stats payload', async () => {
    vi.mocked(api.getProjectStats).mockResolvedValue(makeStats())

    renderPage()

    expect(await screen.findByText('38%')).toBeInTheDocument() // 3 of 8 approved
    expect(screen.getByText('3 of 8 items approved')).toBeInTheDocument()
    expect(screen.getByText('25%')).toBeInTheDocument()
    expect(screen.getByText('1 rejected, 3 approved')).toBeInTheDocument()
    expect(
      screen.getByRole('img', { name: /new 2, prelabeled 0, annotating 1/ }),
    ).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Label counts' })).toHaveTextContent('car12bus3')
    expect(screen.getByRole('row', { name: /Alice/ })).toHaveTextContent('Alice120')
    expect(screen.getByText('5 versions in 2 days')).toBeInTheDocument()
    expect(api.getProjectStats).toHaveBeenCalledWith('p1', { days: 14 })
  })

  it('refetches with the chosen throughput window', async () => {
    vi.mocked(api.getProjectStats).mockResolvedValue(makeStats())
    const user = userEvent.setup()

    renderPage()
    await screen.findByText('38%')
    await user.selectOptions(screen.getByLabelText('Throughput window'), '30')

    await waitFor(() => expect(api.getProjectStats).toHaveBeenCalledWith('p1', { days: 30 }))
  })

  it('shows an empty state for a project without items', async () => {
    vi.mocked(api.getProjectStats).mockResolvedValue(
      makeStats({
        items: {
          total: 0,
          by_status: {
            new: 0,
            prelabeled: 0,
            annotating: 0,
            submitted: 0,
            in_review: 0,
            approved: 0,
            rejected: 0,
            skipped: 0,
          },
        },
      }),
    )

    renderPage()

    expect(await screen.findByText('No items yet')).toBeInTheDocument()
    expect(screen.queryByText('Class balance')).not.toBeInTheDocument()
  })

  it('shows an error state when the request fails', async () => {
    vi.mocked(api.getProjectStats).mockRejectedValue(new Error('boom'))

    renderPage()

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load dashboard')
  })
})
