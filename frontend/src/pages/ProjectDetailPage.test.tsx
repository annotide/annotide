import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ProjectDetailPage } from './ProjectDetailPage'
import { api } from '@/api/client'
import type { CursorPage } from '@/api/client'
import type { Item, Job, Model, ModelVersion, Page, Project, Snapshot, Task } from '@/api/types'
import { DEFAULT_WORKFLOW } from '@/api/types'
import { useAuthStore } from '@/lib/store'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      getProject: vi.fn(),
      listItems: vi.fn(),
      listJobs: vi.fn(),
      createExport: vi.fn(),
      downloadExport: vi.fn(),
      listModels: vi.fn(),
      listModelVersions: vi.fn(),
      prelabelProject: vi.fn(),
      createThumbnails: vi.fn(),
      listTasks: vi.fn(),
      listSnapshots: vi.fn(),
      listMembers: vi.fn(),
      bulkItems: vi.fn(),
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

function makeItem(overrides: Partial<Item> = {}): Item {
  return {
    id: 'i1',
    project_id: 'p1',
    connector_id: 'c1',
    path: 'images/cat.jpg',
    media_type: 'image',
    etag: null,
    size_bytes: 1024,
    width: 800,
    height: 600,
    meta: {},
    status: 'new',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    media_url: 'https://storage.example/cat.jpg?sig=abc',
    ...overrides,
  }
}

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: 'j1',
    project_id: 'p1',
    type: 'export',
    status: 'succeeded',
    progress: 100,
    payload: { format: 'coco' },
    result: null,
    error: null,
    attempts: 1,
    started_at: '2024-01-01T00:00:00Z',
    finished_at: '2024-01-01T00:00:05Z',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:05Z',
    ...overrides,
  }
}

function makeModel(overrides: Partial<Model> = {}): Model {
  return {
    id: 'm1',
    organization_id: 'org1',
    name: 'Detector',
    task: 'detect',
    endpoint_url: 'https://model.example.com',
    identity_type: 'none',
    has_secret: false,
    identity_config: {},
    created_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeModelVersion(overrides: Partial<ModelVersion> = {}): ModelVersion {
  return {
    id: 'mv1',
    model_id: 'm1',
    version: 1,
    snapshot_id: null,
    snapshot_digest: null,
    training_run: null,
    parent_version_id: null,
    derivation: null,
    class_mapping: {},
    metrics: {},
    created_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function emptyPage<T>(): Page<T> {
  return { items: [], next_cursor: null }
}

/**
 * `listJobs` is called once per job-type section (exports, prelabel) with a
 * `type` filter. Route each call to the matching fixture so the two sections
 * never bleed into each other.
 */
function mockJobsByType(byType: { export?: Page<Job>; prelabel?: Page<Job> }): void {
  vi.mocked(api.listJobs).mockImplementation((_projectId: string, filters?: CursorPage) => {
    const type = filters?.type
    if (type === 'prelabel') return Promise.resolve(byType.prelabel ?? emptyPage<Job>())
    if (type === 'snapshot') return Promise.resolve(emptyPage<Job>())
    return Promise.resolve(byType.export ?? emptyPage<Job>())
  })
}

function renderPage(path = '/projects/p1') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/projects/:projectId" element={<ProjectDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ProjectDetailPage', () => {
  beforeEach(() => {
    // Sane defaults so sections unrelated to a given test never error out.
    vi.mocked(api.listModels).mockResolvedValue(emptyPage<Model>())
    vi.mocked(api.listModelVersions).mockResolvedValue(emptyPage<ModelVersion>())
    vi.mocked(api.listTasks).mockResolvedValue(emptyPage<Task>())
    vi.mocked(api.listSnapshots).mockResolvedValue(emptyPage<Snapshot>())
    vi.mocked(api.listMembers).mockResolvedValue([])
    mockJobsByType({})
  })

  afterEach(() => {
    vi.mocked(api.getProject).mockReset()
    vi.mocked(api.listItems).mockReset()
    vi.mocked(api.listJobs).mockReset()
    vi.mocked(api.createExport).mockReset()
    vi.mocked(api.downloadExport).mockReset()
    vi.mocked(api.listModels).mockReset()
    vi.mocked(api.listModelVersions).mockReset()
    vi.mocked(api.prelabelProject).mockReset()
    vi.mocked(api.bulkItems).mockReset()
    useAuthStore.getState().logout()
  })

  it('previews each item in the grid from its signed media URL', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject())
    vi.mocked(api.listItems).mockResolvedValue({ items: [makeItem()], next_cursor: null })

    renderPage()

    const image = await screen.findByRole('presentation')
    expect(image).toHaveAttribute('src', 'https://storage.example/cat.jpg?sig=abc')
    expect(image).toHaveAttribute('loading', 'lazy')
  })

  it('filters the grid by tag on Enter (WF-8)', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject())
    vi.mocked(api.listItems).mockResolvedValue({ items: [makeItem()], next_cursor: null })
    const user = userEvent.setup()

    renderPage()
    await screen.findByRole('presentation')
    await user.type(screen.getByLabelText('Filter by tag'), 'night{Enter}')

    await waitFor(() =>
      expect(api.listItems).toHaveBeenLastCalledWith(
        'p1',
        expect.objectContaining({ tag: 'night' }),
      ),
    )
  })

  it('prefers the generated thumbnail over the full object', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject())
    vi.mocked(api.listItems).mockResolvedValue({
      items: [makeItem({ thumbnail_url: 'https://storage.example/cache/thumbnails/i1.jpg?sig=t' })],
      next_cursor: null,
    })

    renderPage()

    const image = await screen.findByRole('presentation')
    expect(image).toHaveAttribute('src', 'https://storage.example/cache/thumbnails/i1.jpg?sig=t')
  })

  it('queues a thumbnail job from the grid toolbar', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject())
    vi.mocked(api.listItems).mockResolvedValue({ items: [makeItem()], next_cursor: null })
    vi.mocked(api.createThumbnails).mockResolvedValue(
      makeJob({ type: 'thumbnail', status: 'queued' }),
    )
    const user = userEvent.setup()

    renderPage()
    await user.click(await screen.findByRole('button', { name: 'Generate thumbnails' }))

    await waitFor(() => expect(api.createThumbnails).toHaveBeenCalledWith('p1', undefined))
    expect(await screen.findByRole('status')).toHaveTextContent('Thumbnail job queued')
  })

  it('says so when an item has no signable media URL', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject())
    vi.mocked(api.listItems).mockResolvedValue({
      items: [makeItem({ media_url: null })],
      next_cursor: null,
    })

    renderPage()

    expect(await screen.findByText('No preview')).toBeInTheDocument()
    expect(screen.queryByRole('presentation')).not.toBeInTheDocument()
  })

  it('opens on the items tab; the data and job panels are tabs of their own', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject())
    vi.mocked(api.listItems).mockResolvedValue({ items: [makeItem()], next_cursor: null })
    const user = userEvent.setup()

    renderPage()

    await screen.findByRole('presentation')
    expect(screen.getByRole('tab', { name: 'Items' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.queryByRole('button', { name: 'Export' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: 'Exports' }))
    expect(screen.getByRole('button', { name: 'Export' })).toBeInTheDocument()
    expect(screen.queryByRole('presentation')).not.toBeInTheDocument()

    await user.keyboard('{ArrowRight}')
    expect(screen.getByRole('tab', { name: 'Tasks & quality' })).toHaveFocus()
    expect(screen.getByRole('tab', { name: 'Tasks & quality' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
  })

  it('tells a new project how to get items, not that a filter hides them', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject())
    vi.mocked(api.listItems).mockResolvedValue({ items: [], next_cursor: null })
    const user = userEvent.setup()

    renderPage()

    expect(await screen.findByText('No items yet')).toBeInTheDocument()
    expect(screen.queryByText('No items match the current filter.')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Add data' }))
    expect(screen.getByRole('tab', { name: 'Add data' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('heading', { name: 'Scan source storage' })).toBeInTheDocument()
  })

  it('renders links to start annotating and start reviewing', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject())
    vi.mocked(api.listItems).mockResolvedValue({ items: [], next_cursor: null })

    renderPage()

    await waitFor(() => {
      expect(screen.getByText('Demo project')).toBeInTheDocument()
    })

    expect(screen.getByRole('link', { name: 'Start annotating' })).toHaveAttribute(
      'href',
      '/projects/p1/annotate',
    )
    expect(screen.getByRole('link', { name: 'Start reviewing' })).toHaveAttribute(
      'href',
      '/projects/p1/review',
    )
  })

  it('creates an export with the chosen format', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject())
    vi.mocked(api.listItems).mockResolvedValue({ items: [], next_cursor: null })
    vi.mocked(api.createExport).mockResolvedValue(makeJob({ payload: { format: 'yolo' } }))

    renderPage('/projects/p1?tab=exports')

    await waitFor(() => {
      expect(screen.getByText('No exports yet.')).toBeInTheDocument()
    })

    const user = userEvent.setup()
    await user.selectOptions(screen.getByLabelText('Format'), 'yolo')
    await user.click(screen.getByRole('button', { name: 'Export' }))

    await waitFor(() => {
      expect(api.createExport).toHaveBeenCalledWith('p1', { format: 'yolo' })
    })
  })

  it('downloads a succeeded export job', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject())
    vi.mocked(api.listItems).mockResolvedValue({ items: [], next_cursor: null })
    mockJobsByType({ export: { items: [makeJob()], next_cursor: null } })
    vi.mocked(api.downloadExport).mockResolvedValue({
      url: 'https://example.com/export.zip',
      expires_in: 900,
    })
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null)

    renderPage('/projects/p1?tab=exports')

    const downloadButton = await screen.findByRole('button', { name: 'Download' })
    const user = userEvent.setup()
    await user.click(downloadButton)

    await waitFor(() => {
      expect(api.downloadExport).toHaveBeenCalledWith('j1')
    })
    await waitFor(() => {
      expect(openSpy).toHaveBeenCalledWith('https://example.com/export.zip', '_blank', 'noopener')
    })

    openSpy.mockRestore()
  })

  it('defaults the version select to the highest version and pre-labels with a limit', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject())
    vi.mocked(api.listItems).mockResolvedValue({ items: [], next_cursor: null })
    vi.mocked(api.listModels).mockResolvedValue({ items: [makeModel()], next_cursor: null })
    vi.mocked(api.listModelVersions).mockResolvedValue({
      items: [
        makeModelVersion({ id: 'mv1', version: 1 }),
        makeModelVersion({ id: 'mv2', version: 2 }),
      ],
      next_cursor: null,
    })
    vi.mocked(api.prelabelProject).mockResolvedValue(
      makeJob({ id: 'j2', type: 'prelabel', payload: {} }),
    )

    renderPage('/projects/p1?tab=prelabel')

    const versionSelect = await screen.findByLabelText<HTMLSelectElement>('Version')
    await waitFor(() => {
      expect(versionSelect.value).toBe('mv2')
    })

    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Limit (dry run)'), '25')
    await user.click(screen.getByRole('button', { name: 'Pre-label' }))

    await waitFor(() => {
      expect(api.prelabelProject).toHaveBeenCalledWith('p1', {
        model_version_id: 'mv2',
        limit: 25,
        confidence_threshold: 0,
        prioritize_uncertain: undefined,
      })
    })

    // ML-6: opting in sends the flag.
    await user.click(screen.getByLabelText('Queue uncertain first'))
    await user.click(screen.getByRole('button', { name: 'Pre-label' }))
    await waitFor(() => {
      expect(api.prelabelProject).toHaveBeenLastCalledWith(
        'p1',
        expect.objectContaining({ prioritize_uncertain: true }),
      )
    })
  })

  it('shows "No models registered." and disables pre-labelling when there are none', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject())
    vi.mocked(api.listItems).mockResolvedValue({ items: [], next_cursor: null })

    renderPage('/projects/p1?tab=prelabel')

    await waitFor(() => {
      expect(screen.getByText('No models registered.')).toBeInTheDocument()
    })
    expect(screen.getByRole('button', { name: 'Pre-label' })).toBeDisabled()
  })

  it('renders a summary for a succeeded pre-label job', async () => {
    vi.mocked(api.getProject).mockResolvedValue(makeProject())
    vi.mocked(api.listItems).mockResolvedValue({ items: [], next_cursor: null })
    vi.mocked(api.listModels).mockResolvedValue({ items: [makeModel()], next_cursor: null })
    vi.mocked(api.listModelVersions).mockResolvedValue({
      items: [makeModelVersion()],
      next_cursor: null,
    })
    mockJobsByType({
      prelabel: {
        items: [
          makeJob({
            id: 'j3',
            type: 'prelabel',
            payload: {},
            result: {
              model_version_id: 'mv1',
              selected: 20,
              predicted: 18,
              empty: 1,
              skipped_human: 1,
              errors: 0,
            },
          }),
        ],
        next_cursor: null,
      },
    })

    renderPage()

    await waitFor(() => {
      expect(
        screen.getByText('18/20 predicted, 1 empty, 1 skipped (human), 0 errors'),
      ).toBeInTheDocument()
    })
  })

  describe('bulk actions (WF-8)', () => {
    function signInAs(role: 'owner' | 'annotator'): void {
      useAuthStore.getState().login('t', {
        id: 'u1',
        organization_id: 'org1',
        email: 'me@example.com',
        display_name: 'Me',
        is_active: true,
        is_superuser: false,
        last_seen_at: null,
        created_at: '2024-01-01T00:00:00Z',
        updated_at: '2024-01-01T00:00:00Z',
      })
      vi.mocked(api.listMembers).mockResolvedValue([
        {
          user_id: 'u1',
          email: 'me@example.com',
          display_name: 'Me',
          role,
          created_at: '2024-01-01T00:00:00Z',
        },
      ])
    }

    it('hides selection from annotators', async () => {
      signInAs('annotator')
      vi.mocked(api.getProject).mockResolvedValue(makeProject())
      vi.mocked(api.listItems).mockResolvedValue({ items: [makeItem()], next_cursor: null })

      renderPage()

      await screen.findByRole('presentation')
      expect(screen.queryByLabelText('Select images/cat.jpg')).not.toBeInTheDocument()
      expect(screen.queryByLabelText('Select all loaded items')).not.toBeInTheDocument()
    })

    it('lets an owner select items and tag them in one request', async () => {
      signInAs('owner')
      vi.mocked(api.getProject).mockResolvedValue(makeProject())
      vi.mocked(api.listItems).mockResolvedValue({
        items: [makeItem(), makeItem({ id: 'i2', path: 'images/dog.jpg' })],
        next_cursor: null,
      })
      vi.mocked(api.bulkItems).mockResolvedValue({
        applied: 1,
        skipped: [{ item_id: 'i2', reason: 'tags unchanged' }],
      })
      const user = userEvent.setup()

      renderPage()

      await user.click(await screen.findByLabelText('Select images/cat.jpg'))
      expect(screen.getByTestId('bulk-count')).toHaveTextContent('1 selected')

      await user.click(screen.getByLabelText('Select all loaded items'))
      expect(screen.getByTestId('bulk-count')).toHaveTextContent('2 selected')

      await user.selectOptions(screen.getByLabelText('Bulk action'), 'tag')
      await user.type(screen.getByLabelText('Tags to add'), 'night, blurry')
      await user.click(screen.getByRole('button', { name: 'Apply' }))

      await waitFor(() => {
        expect(api.bulkItems).toHaveBeenCalledWith('p1', {
          action: 'tag',
          item_ids: ['i1', 'i2'],
          add: ['night', 'blurry'],
          remove: [],
        })
      })
      // The grid restarts from page one and the selection is dropped.
      await waitFor(() => {
        expect(screen.queryByTestId('bulk-count')).not.toBeInTheDocument()
      })
    })

    it('shows tags from item.meta on the card', async () => {
      signInAs('owner')
      vi.mocked(api.getProject).mockResolvedValue(makeProject())
      vi.mocked(api.listItems).mockResolvedValue({
        items: [makeItem({ meta: { tags: ['night', 'rain'] } })],
        next_cursor: null,
      })

      renderPage()

      const tags = await screen.findByRole('list', { name: 'Tags' })
      expect(tags).toHaveTextContent('night')
      expect(tags).toHaveTextContent('rain')
    })
  })
})
