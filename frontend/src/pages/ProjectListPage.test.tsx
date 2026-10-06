import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ReactElement } from 'react'
import { ProjectListPage } from './ProjectListPage'
import { api } from '@/api/client'
import type { Project } from '@/api/types'
import { DEFAULT_WORKFLOW } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      listProjects: vi.fn(),
      createProject: vi.fn(),
    },
  }
})

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'p1',
    organization_id: 'org1',
    name: 'Demo project',
    description: 'A demo project',
    label_schema_id: null,
    source_connector_id: null,
    result_connector_id: null,
    cache_connector_id: null,
    source_prefix: null,
    source_glob: null,
    workflow: DEFAULT_WORKFLOW,
    settings: {},
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderPage(ui: ReactElement) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ProjectListPage', () => {
  afterEach(() => {
    vi.mocked(api.listProjects).mockReset()
    vi.mocked(api.createProject).mockReset()
  })

  it('creates a project from the inline form and opens its settings', async () => {
    vi.mocked(api.listProjects).mockResolvedValue({ items: [], next_cursor: null })
    vi.mocked(api.createProject).mockResolvedValue(makeProject({ id: 'p9', name: 'Fresh' }))
    const user = userEvent.setup()

    renderPage(
      <Routes>
        <Route path="/" element={<ProjectListPage />} />
        <Route path="/projects/:projectId/settings" element={<p>settings for p9</p>} />
      </Routes>,
    )

    await user.click(screen.getByRole('button', { name: 'New project' }))
    await user.type(screen.getByLabelText('Name'), '  Fresh  ')
    await user.type(screen.getByLabelText('Description'), 'first one')
    await user.click(screen.getByRole('button', { name: 'Create project' }))

    await waitFor(() => {
      expect(api.createProject).toHaveBeenCalledWith({ name: 'Fresh', description: 'first one' })
    })
    expect(await screen.findByText('settings for p9')).toBeInTheDocument()
  })

  it('does not submit an empty name', async () => {
    vi.mocked(api.listProjects).mockResolvedValue({ items: [], next_cursor: null })
    const user = userEvent.setup()

    renderPage(<ProjectListPage />)

    await user.click(screen.getByRole('button', { name: 'New project' }))
    expect(screen.getByRole('button', { name: 'Create project' })).toBeDisabled()
    expect(api.createProject).not.toHaveBeenCalled()
  })

  it('shows a loading state while the query is pending', () => {
    vi.mocked(api.listProjects).mockReturnValue(new Promise(() => {}))

    renderPage(<ProjectListPage />)

    expect(screen.getByRole('status')).toHaveTextContent('Loading projects…')
  })

  it('renders the list of projects once loaded', async () => {
    vi.mocked(api.listProjects).mockResolvedValue({
      items: [makeProject()],
      next_cursor: null,
    })

    renderPage(<ProjectListPage />)

    await waitFor(() => {
      expect(screen.getByText('Demo project')).toBeInTheDocument()
    })
  })

  it('shows an empty state when there are no projects', async () => {
    vi.mocked(api.listProjects).mockResolvedValue({ items: [], next_cursor: null })

    renderPage(<ProjectListPage />)

    await waitFor(() => {
      expect(screen.getByText('No projects yet')).toBeInTheDocument()
    })
  })
})
