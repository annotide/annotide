import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ProjectSettingsPage } from './ProjectSettingsPage'
import { api } from '@/api/client'
import type { Connector, Job, Member, Page, Project, User } from '@/api/types'
import { DEFAULT_WORKFLOW } from '@/api/types'
import { useAuthStore } from '@/lib/store'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      getProject: vi.fn(),
      updateProject: vi.fn(),
      deleteProject: vi.fn(),
      listConnectors: vi.fn(),
      listSchemas: vi.fn(),
      createSchemaVersion: vi.fn(),
      listMembers: vi.fn(),
      addMember: vi.fn(),
      updateMember: vi.fn(),
      removeMember: vi.fn(),
      listWebhooks: vi.fn(),
      rebuildCache: vi.fn(),
      extractPdfText: vi.fn(),
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

function makeUser(overrides: Partial<User> = {}): User {
  return {
    id: 'u1',
    organization_id: 'org1',
    email: 'owner@example.com',
    display_name: 'Owner',
    is_active: true,
    is_superuser: false,
    last_seen_at: null,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeMember(overrides: Partial<Member> = {}): Member {
  return {
    user_id: 'u1',
    email: 'owner@example.com',
    display_name: 'Owner',
    role: 'owner',
    created_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function emptyConnectors(): Page<Connector> {
  return { items: [], next_cursor: null }
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/projects/p1/settings']}>
        <Routes>
          <Route path="/projects/:projectId/settings" element={<ProjectSettingsPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ProjectSettingsPage', () => {
  beforeEach(() => {
    useAuthStore.getState().login('t', makeUser())
    vi.mocked(api.listConnectors).mockResolvedValue(emptyConnectors())
    vi.mocked(api.listSchemas).mockResolvedValue([])
    vi.mocked(api.listMembers).mockResolvedValue([makeMember()])
    vi.mocked(api.listWebhooks).mockResolvedValue({ items: [], next_cursor: null })
  })

  afterEach(() => {
    useAuthStore.getState().logout()
    vi.mocked(api.getProject).mockReset()
    vi.mocked(api.updateProject).mockReset()
    vi.mocked(api.deleteProject).mockReset()
    vi.mocked(api.listConnectors).mockReset()
    vi.mocked(api.listSchemas).mockReset()
    vi.mocked(api.listMembers).mockReset()
    vi.mocked(api.listWebhooks).mockReset()
    vi.mocked(api.rebuildCache).mockReset()
  })

  it('submits a PATCH with the edited general fields', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject())
    vi.mocked(api.updateProject).mockResolvedValue(makeProject({ name: 'Renamed project' }))
    const user = userEvent.setup()

    renderPage()

    const nameInput = await screen.findByLabelText('name')
    await user.clear(nameInput)
    await user.type(nameInput, 'Renamed project')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(api.updateProject).toHaveBeenCalledWith('p1', {
        name: 'Renamed project',
        description: 'A demo project',
        source_connector_id: null,
        result_connector_id: null,
        cache_connector_id: null,
        source_prefix: null,
        source_glob: null,
      })
    })
    expect(await screen.findByText('Saved.')).toBeInTheDocument()
  })

  it('saves companion extensions into settings without touching other keys', async () => {
    vi.mocked(api.getProject).mockResolvedValue(
      makeProject({ settings: { calibration: { units_per_pixel: 0.5, unit: 'mm' } } }),
    )
    vi.mocked(api.updateProject).mockResolvedValue(makeProject())
    const user = userEvent.setup()
    renderPage()

    const field = await screen.findByLabelText('Companion file extensions')
    await waitFor(() => expect(field).toHaveValue(''))
    await user.type(field, 'TXT, .json')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(api.updateProject).toHaveBeenCalledWith(
        'p1',
        expect.objectContaining({
          settings: {
            calibration: { units_per_pixel: 0.5, unit: 'mm' },
            companion_extensions: ['.txt', '.json'],
          },
        }),
      )
    })
  })

  it('switches PDFs to text mode and back, keeping other settings', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject({ settings: { companion_extensions: ['.txt'] } }))
    vi.mocked(api.updateProject).mockResolvedValue(makeProject())
    const user = userEvent.setup()
    renderPage()

    const select = await screen.findByLabelText('PDF files')
    await waitFor(() => expect(select).toHaveValue('layout'))
    await user.selectOptions(select, 'text')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(api.updateProject).toHaveBeenCalledWith(
        'p1',
        expect.objectContaining({ settings: { companion_extensions: ['.txt'], pdf_mode: 'text' } }),
      )
    })
  })

  it('removes the PDF mode when set back to layout', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject({ settings: { pdf_mode: 'text' } }))
    vi.mocked(api.updateProject).mockResolvedValue(makeProject())
    const user = userEvent.setup()
    renderPage()

    const select = await screen.findByLabelText('PDF files')
    await waitFor(() => expect(select).toHaveValue('text'))
    await user.selectOptions(select, 'layout')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => {
      expect(api.updateProject).toHaveBeenCalledWith(
        'p1',
        expect.objectContaining({ settings: { companion_extensions: null, pdf_mode: null } }),
      )
    })
  })

  it('queues a cache rebuild with purge once the project has somewhere to write it', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject({ result_connector_id: 'c1' }))
    vi.mocked(api.rebuildCache).mockResolvedValue({ id: 'j1' } as Job)
    const user = userEvent.setup()

    renderPage()

    await user.click(await screen.findByLabelText('Delete old files first'))
    await user.click(screen.getByRole('button', { name: 'Rebuild cache' }))

    await waitFor(() => {
      expect(api.rebuildCache).toHaveBeenCalledWith('p1', true)
    })
    expect(await screen.findByText('Rebuild queued.')).toBeInTheDocument()
  })

  it('queues PDF text extraction in a text-mode project', async () => {
    vi.mocked(api.getProject).mockResolvedValue(
      makeProject({ result_connector_id: 'c1', settings: { pdf_mode: 'text' } }),
    )
    vi.mocked(api.extractPdfText).mockResolvedValue({ id: 'j2' } as Job)
    const user = userEvent.setup()

    renderPage()

    await user.click(await screen.findByLabelText('Also redo PDFs nobody has annotated'))
    await user.click(screen.getByRole('button', { name: 'Extract PDF text' }))

    await waitFor(() => {
      expect(api.extractPdfText).toHaveBeenCalledWith('p1', true)
    })
    expect(await screen.findByText('Extraction queued.')).toBeInTheDocument()
  })

  it('offers no PDF text extraction in a layout project', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject({ result_connector_id: 'c1' }))

    renderPage()

    await screen.findByRole('button', { name: 'Rebuild cache' })
    expect(screen.queryByRole('button', { name: 'Extract PDF text' })).not.toBeInTheDocument()
  })

  it('offers no rebuild without a result or cache connector', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject())

    renderPage()

    await screen.findByLabelText('Cache connector')
    expect(screen.queryByRole('button', { name: 'Rebuild cache' })).not.toBeInTheDocument()
  })

  it('deletes the project after confirming, then navigates away', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject())
    vi.mocked(api.deleteProject).mockResolvedValue(undefined)
    const user = userEvent.setup()

    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Delete project' }))
    await user.click(screen.getByRole('button', { name: 'Confirm delete' }))

    await waitFor(() => {
      expect(api.deleteProject).toHaveBeenCalledWith('p1')
    })
  })

  it('renders the page read-only for a non-owner', async () => {
    useAuthStore.getState().login('t', makeUser({ id: 'u2', display_name: 'Annotator' }))
    vi.mocked(api.listMembers).mockResolvedValue([
      makeMember({ user_id: 'u2', display_name: 'Annotator', email: 'a@example.com', role: 'annotator' }),
    ])
    vi.mocked(api.getProject).mockResolvedValue(makeProject())

    renderPage()

    await screen.findByLabelText('name')
    expect(screen.getByLabelText('name')).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete project' })).not.toBeInTheDocument()
  })
})
