import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AnnotatePage } from './AnnotatePage'
import { api, ApiError } from '@/api/client'
import type {
  Annotation,
  AnnotationResult,
  Item,
  LabelSchemaVersion,
  Model,
  Project,
  Task,
} from '@/api/types'
import { useTaskStore, useRecentItemStore } from '@/lib/store'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      listItems: vi.fn(),
      getItem: vi.fn(),
      listSchemas: vi.fn(),
      nextTask: vi.fn(),
      releaseTask: vi.fn(),
      extendTask: vi.fn(),
      createAnnotation: vi.fn(),
      listAnnotations: vi.fn(),
      listComments: vi.fn().mockResolvedValue([]),
      createComment: vi.fn(),
      resolveComment: vi.fn(),
      listModels: vi.fn(),
      interactiveSegment: vi.fn(),
      getProject: vi.fn(),
      updateProject: vi.fn(),
      fetchMediaText: vi.fn(),
      signTiles: vi.fn(),
    },
  }
})

// The Konva canvas needs a real DOM canvas; the page only forwards props to
// it. The stub exposes two buttons that stand in for a drag and a click on
// the canvas so the page's attribute editor (TOOL-2) can be driven.
vi.mock('@/features/annotator', () => ({
  ImageAnnotator: ({
    value,
    onChange,
    onSelectionChange,
    onSmartPrompt,
    onCalibrate,
    tiles,
    signTiles,
  }: {
    value: AnnotationResult
    onChange: (next: AnnotationResult) => void
    onCalibrate?: (unitsPerPixel: number, unit: string) => void
    onSelectionChange?: (id: string | null) => void
    onSmartPrompt?: (prompt: { kind: 'point'; point: [number, number] }) => Promise<unknown>
    tiles?: { max_level: number } | null
    signTiles?: (tiles: [number, number, number][]) => Promise<string[]>
  }) => (
    <div data-testid="annotator" data-tiles={tiles ? String(tiles.max_level) : 'none'}>
      {signTiles && (
        <button type="button" onClick={() => void signTiles([[0, 0, 0]])}>
          stub-sign
        </button>
      )}
      {onSmartPrompt && (
        <button
          type="button"
          onClick={() =>
            void onSmartPrompt({ kind: 'point', point: [3, 4] }).catch(() => undefined)
          }
        >
          stub-smart
        </button>
      )}
      <button
        type="button"
        onClick={() =>
          onChange({
            ...value,
            shapes: [
              ...value.shapes,
              {
                id: 'shape-1',
                type: 'bbox',
                class: 'car',
                attributes: { occluded: false },
                confidence: null,
                bbox: [1, 1, 10, 10],
              },
            ],
          })
        }
      >
        stub-draw
      </button>
      <button type="button" onClick={() => onSelectionChange?.('shape-1')}>
        stub-select
      </button>
      <button type="button" onClick={() => onCalibrate?.(0.5, 'mm')}>
        stub-calibrate
      </button>
    </div>
  ),
}))

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 't1',
    item_id: 'i1',
    project_id: 'p1',
    type: 'annotate',
    assignee_id: 'u1',
    status: 'in_progress',
    locked_by_id: 'u1',
    locked_until: '2026-09-18T10:00:00Z',
    priority: 0,
    deadline: null,
    slot: null,
    region: null,
    gold: false,
    created_at: '2026-09-18T09:00:00Z',
    updated_at: '2026-09-18T09:00:00Z',
    ...overrides,
  }
}

function makeModel(overrides: Partial<Model> = {}): Model {
  return {
    id: 'm1',
    organization_id: 'o1',
    name: 'SAM',
    task: 'segment',
    endpoint_url: 'http://model:8000',
    identity_type: 'none',
    has_secret: false,
    identity_config: {},
    created_at: '2026-09-18T09:00:00Z',
    ...overrides,
  }
}

function makeItem(overrides: Partial<Item> = {}): Item {
  return {
    id: 'i1',
    project_id: 'p1',
    connector_id: 'c1',
    path: 'images/a.jpg',
    media_type: 'image',
    etag: null,
    size_bytes: 10,
    width: 100,
    height: 80,
    meta: {},
    status: 'new',
    created_at: '2026-09-18T09:00:00Z',
    updated_at: '2026-09-18T09:00:00Z',
    media_url: 'https://media.example/a.jpg',
    ...overrides,
  }
}

const schema: LabelSchemaVersion = {
  id: 's1',
  label_schema_id: 'ls1',
  version: 1,
  definition: {
    version: 1,
    classes: [
      {
        name: 'car',
        display_name: 'Car',
        color: '#f00',
        tools: ['bbox'],
        attributes: [
          { name: 'occluded', type: 'boolean', required: false, default: false },
          { name: 'make', type: 'select', required: false, options: ['Toyota', 'Volvo'] },
        ],
      },
    ],
    classification: [
      { name: 'weather', type: 'select', required: false, options: ['clear', 'rain'] },
    ],
  },
  created_at: '2026-09-18T09:00:00Z',
}

const annotation: Annotation = {
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
  duration_ms: 10,
  blob_path: null,
  kind: 'primary',
  created_at: '2026-09-18T09:00:00Z',
}

function renderAt(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/projects/:projectId/annotate/:itemId?" element={<AnnotatePage />} />
          <Route path="/projects/:projectId" element={<div>project page</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('AnnotatePage', () => {
  beforeEach(() => {
    useTaskStore.getState().clearTask()
    vi.mocked(api.listItems).mockResolvedValue({ items: [makeItem()], next_cursor: null })
    vi.mocked(api.getItem).mockResolvedValue(makeItem())
    vi.mocked(api.listSchemas).mockResolvedValue([schema])
    vi.mocked(api.releaseTask).mockResolvedValue(makeTask({ status: 'open' }))
    vi.mocked(api.createAnnotation).mockResolvedValue(annotation)
    vi.mocked(api.listAnnotations).mockResolvedValue([])
    vi.mocked(api.listModels).mockResolvedValue({ items: [], next_cursor: null })
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('claims the next annotate task in queue mode and opens its item', async () => {
    vi.mocked(api.nextTask).mockResolvedValue(makeTask())

    renderAt('/projects/p1/annotate')

    await waitFor(() => expect(screen.getByTestId('annotator')).toBeInTheDocument())
    expect(api.nextTask).toHaveBeenCalledWith({ project_id: 'p1', type: 'annotate' })
    expect(api.getItem).toHaveBeenCalledWith('i1')
    expect(screen.getByText(/locked until/)).toBeInTheDocument()
  })

  it('shows the empty state and re-claims on "Check again"', async () => {
    vi.mocked(api.nextTask).mockResolvedValue(null)
    const user = userEvent.setup()

    renderAt('/projects/p1/annotate')

    await waitFor(() => expect(screen.getByText('Queue is empty')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(api.nextTask).toHaveBeenCalledTimes(2))
  })

  it('submits with task_id and submit=true, then returns to the queue', async () => {
    vi.mocked(api.nextTask).mockResolvedValueOnce(makeTask()).mockResolvedValueOnce(null)
    const user = userEvent.setup()

    renderAt('/projects/p1/annotate')
    await waitFor(() => expect(screen.getByTestId('annotator')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Submit' }))

    await waitFor(() => expect(api.createAnnotation).toHaveBeenCalled())
    const [itemId, payload] = vi.mocked(api.createAnnotation).mock.calls[0]
    expect(itemId).toBe('i1')
    expect(payload).toMatchObject({ task_id: 't1', submit: true, label_schema_version_id: 's1' })
    expect(typeof payload.duration_ms).toBe('number')

    // Back in queue mode: a second claim, and no release (the server closed the task).
    await waitFor(() => expect(api.nextTask).toHaveBeenCalledTimes(2))
    expect(api.releaseTask).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getByText('Queue is empty')).toBeInTheDocument())
  })

  it('stops a submit that misses a required attribute and says which (QA-6)', async () => {
    vi.mocked(api.listSchemas).mockResolvedValue([
      {
        ...schema,
        definition: {
          ...schema.definition,
          classification: [
            { name: 'weather', type: 'select', required: true, options: ['clear', 'rain'] },
          ],
        },
      },
    ])
    const user = userEvent.setup()
    renderAt('/projects/p1/annotate/i1')
    await waitFor(() => expect(screen.getByTestId('annotator')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Submit' }))
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Fill in the required attribute before submitting: weather',
    )
    expect(api.createAnnotation).not.toHaveBeenCalled()

    await user.selectOptions(screen.getByLabelText(/weather/), 'rain')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Submit' }))
    await waitFor(() => expect(api.createAnnotation).toHaveBeenCalled())
  })

  it('skip in task mode releases the lock and claims again', async () => {
    vi.mocked(api.nextTask).mockResolvedValueOnce(makeTask()).mockResolvedValueOnce(null)
    const user = userEvent.setup()

    renderAt('/projects/p1/annotate')
    await waitFor(() => expect(screen.getByTestId('annotator')).toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Skip' }))

    await waitFor(() => expect(api.releaseTask).toHaveBeenCalledWith('t1'))
    await waitFor(() => expect(api.nextTask).toHaveBeenCalledTimes(2))
  })

  it('browsing an item directly takes no lock and pages by offset', async () => {
    vi.mocked(api.listItems).mockResolvedValue({
      items: [makeItem(), makeItem({ id: 'i2', path: 'images/b.jpg' })],
      next_cursor: null,
    })
    vi.mocked(api.getItem).mockImplementation((id) =>
      Promise.resolve(makeItem({ id, path: id === 'i2' ? 'images/b.jpg' : 'images/a.jpg' })),
    )
    const user = userEvent.setup()

    renderAt('/projects/p1/annotate/i1')
    await waitFor(() => expect(screen.getByTestId('annotator')).toBeInTheDocument())
    expect(api.nextTask).not.toHaveBeenCalled()
    expect(screen.queryByText(/locked until/)).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Skip' }))
    await waitFor(() => expect(api.getItem).toHaveBeenCalledWith('i2'))
    expect(api.releaseTask).not.toHaveBeenCalled()
  })

  it('asks before leaving an item with unsaved shapes, not after a save', async () => {
    vi.mocked(api.listItems).mockResolvedValue({
      items: [makeItem(), makeItem({ id: 'i2', path: 'images/b.jpg' })],
      next_cursor: null,
    })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const user = userEvent.setup()

    renderAt('/projects/p1/annotate/i1')
    await waitFor(() => expect(screen.getByTestId('annotator')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'stub-draw' }))

    await user.click(screen.getByRole('button', { name: 'Skip' }))
    expect(confirm).toHaveBeenCalledWith('This item has unsaved changes. Leave without saving?')
    expect(api.getItem).not.toHaveBeenCalledWith('i2')

    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(() => expect(api.createAnnotation).toHaveBeenCalled())
    confirm.mockClear()
    await user.click(screen.getByRole('button', { name: 'Skip' }))
    await waitFor(() => expect(api.getItem).toHaveBeenCalledWith('i2'))
    expect(confirm).not.toHaveBeenCalled()
    confirm.mockRestore()
  })

  it('leaves a key the annotator consumed to the annotator (P is also the point tool)', async () => {
    vi.mocked(api.listItems).mockResolvedValue({
      items: [makeItem(), makeItem({ id: 'i2', path: 'images/b.jpg' })],
      next_cursor: null,
    })
    renderAt('/projects/p1/annotate/i2')
    await waitFor(() => expect(screen.getByTestId('annotator')).toBeInTheDocument())

    // The real canvas marks the tool key consumed; stand in for it here.
    const consume = (event: KeyboardEvent): void => event.preventDefault()
    window.addEventListener('keydown', consume)
    fireEvent.keyDown(window, { key: 'p' })
    window.removeEventListener('keydown', consume)
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(api.getItem).not.toHaveBeenCalledWith('i1')

    fireEvent.keyDown(window, { key: 'p' })
    await waitFor(() => expect(api.getItem).toHaveBeenCalledWith('i1'))
  })

  it('lists every shortcut on ? and keeps keys inside the sheet', async () => {
    vi.mocked(api.listItems).mockResolvedValue({
      items: [makeItem(), makeItem({ id: 'i2', path: 'images/b.jpg' })],
      next_cursor: null,
    })
    const user = userEvent.setup()
    renderAt('/projects/p1/annotate/i2')
    await waitFor(() => expect(screen.getByTestId('annotator')).toBeInTheDocument())

    await user.keyboard('?')
    const dialog = await screen.findByRole('dialog', { name: 'Keyboard shortcuts' })
    // On an image P is the point tool, so paging back takes Shift.
    expect(dialog).toHaveTextContent('Shift+PPrevious item')
    expect(dialog).toHaveTextContent('Polygon')
    expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus()

    await user.keyboard('p')
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(api.getItem).not.toHaveBeenCalledWith('i1')

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('edits classification and selected-shape attributes into the saved result', async () => {
    const user = userEvent.setup()
    renderAt('/projects/p1/annotate/i1')
    await waitFor(() => expect(screen.getByTestId('annotator')).toBeInTheDocument())

    expect(screen.getByText('Select a shape to edit its attributes.')).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('weather'), 'rain')

    await user.click(screen.getByRole('button', { name: 'stub-draw' }))
    await user.click(screen.getByRole('button', { name: 'stub-select' }))
    expect(screen.getByRole('heading', { name: 'Annotations (1)' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Selected shape' })).toBeInTheDocument()
    expect(screen.getByLabelText('occluded')).not.toBeChecked()

    await user.click(screen.getByLabelText('occluded'))
    await user.selectOptions(screen.getByLabelText('make'), 'Volvo')

    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(() => expect(api.createAnnotation).toHaveBeenCalled())
    const [, body] = vi.mocked(api.createAnnotation).mock.calls[0]
    expect(body.result.classification).toEqual({ weather: 'rain' })
    expect(body.result.shapes[0].attributes).toEqual({ occluded: true, make: 'Volvo' })
  })

  it('tells the user when the connector cannot sign a media URL', async () => {
    vi.mocked(api.getItem).mockResolvedValue(makeItem({ media_url: null }))

    renderAt('/projects/p1/annotate/i1')

    await waitFor(() =>
      expect(screen.getByText('No preview available for this connector.')).toBeInTheDocument(),
    )
    expect(screen.queryByTestId('annotator')).not.toBeInTheDocument()
  })

  it('opens the item on its latest version, e.g. a model draft (ML-2)', async () => {
    const box = (id: string) => ({
      id,
      type: 'bbox' as const,
      class: 'car',
      attributes: {},
      confidence: 0.6,
      bbox: [1, 2, 3, 4] as [number, number, number, number],
    })
    vi.mocked(api.listAnnotations).mockResolvedValue([
      { ...annotation, id: 'a1', version: 1, result: { ...annotation.result, shapes: [box('old')] } },
      {
        ...annotation,
        id: 'a2',
        version: 2,
        source: 'model',
        status: 'draft',
        author_user_id: null,
        author_model_version_id: 'mv1',
        result: { ...annotation.result, shapes: [box('m1'), box('m2')] },
      },
    ])

    renderAt('/projects/p1/annotate/i1')

    await screen.findByTestId('annotator')
    expect(screen.getByRole('heading', { name: 'Annotations (2)' })).toBeInTheDocument()
  })

  it('does not open an empty canvas when the versions cannot be loaded', async () => {
    vi.mocked(api.listAnnotations).mockRejectedValue(new Error('boom'))

    renderAt('/projects/p1/annotate/i1')

    await screen.findByText('Could not load annotations')
    expect(screen.queryByTestId('annotator')).not.toBeInTheDocument()
  })

  it('offers the smart polygon only when a segment model exists (ML-7)', async () => {
    renderAt('/projects/p1/annotate/i1')
    await screen.findByTestId('annotator')
    expect(screen.queryByText('Smart polygon')).not.toBeInTheDocument()
    expect(screen.queryByText('stub-smart')).not.toBeInTheDocument()
  })

  it('sends a smart prompt to the segment model and surfaces its failure', async () => {
    vi.mocked(api.listModels).mockResolvedValue({
      items: [makeModel({ id: 'd1', name: 'Detector', task: 'detect' }), makeModel()],
      next_cursor: null,
    })
    vi.mocked(api.interactiveSegment).mockResolvedValueOnce({
      type: 'polygon',
      points: [
        [1, 1],
        [5, 1],
        [5, 5],
      ],
      confidence: 0.7,
    })
    renderAt('/projects/p1/annotate/i1')
    const user = userEvent.setup()

    expect(await screen.findByText('Smart polygon')).toBeInTheDocument()
    expect(screen.getByText('SAM')).toBeInTheDocument()
    expect(screen.queryByText('Detector')).not.toBeInTheDocument()

    await user.click(screen.getByText('stub-smart'))
    await waitFor(() =>
      expect(api.interactiveSegment).toHaveBeenCalledWith('i1', {
        model_id: 'm1',
        point: { x: 3, y: 4 },
      }),
    )
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    vi.mocked(api.interactiveSegment).mockRejectedValueOnce(
      new ApiError({
        type: 'urn:problem:model-unavailable',
        title: 'Model endpoint unavailable',
        status: 503,
        detail: 'POST /interactive: model down',
      }),
    )
    await user.click(screen.getByText('stub-smart'))
    expect(await screen.findByRole('alert')).toHaveTextContent('model down')
  })

  it('remembers a saved item and copies its shapes onto the next one (TOOL-7)', async () => {
    const car = {
      id: 'old-1',
      type: 'bbox' as const,
      class: 'car',
      attributes: { occluded: true },
      confidence: null,
      bbox: [5, 5, 20, 20] as [number, number, number, number],
    }
    const gone = { ...car, id: 'old-2', class: 'truck' }
    vi.mocked(api.listAnnotations).mockImplementation(async (itemId: string) =>
      itemId === 'i0'
        ? [
            {
              ...annotation,
              item_id: 'i0',
              result: {
                ...annotation.result,
                classification: { weather: 'rain' },
                shapes: [car, gone],
              },
            },
          ]
        : [],
    )
    useRecentItemStore.setState({ lastSavedItem: {} })
    const user = userEvent.setup()

    renderAt('/projects/p1/annotate/i1')
    await waitFor(() => expect(screen.getByTestId('annotator')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Copy from previous' })).toBeDisabled()

    // Saving an item makes it the copy source for the next one.
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(() => expect(useRecentItemStore.getState().lastSavedItem).toEqual({ p1: 'i1' }))

    useRecentItemStore.setState({ lastSavedItem: { p1: 'i0' } })
    const copy = await screen.findByRole('button', { name: 'Copy from previous' })
    await waitFor(() => expect(copy).toBeEnabled())
    await user.click(copy)

    expect(screen.getByText('Annotations (1)')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(
      'Copied 1 shape; 1 skipped (class no longer in the schema).',
    )
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(() => expect(api.createAnnotation).toHaveBeenCalledTimes(2))
    const saved = vi.mocked(api.createAnnotation).mock.calls[1][1].result
    expect(saved.classification).toEqual({ weather: 'rain' })
    expect(saved.shapes).toHaveLength(1)
    expect(saved.shapes[0]).toMatchObject({ class: 'car', bbox: [5, 5, 20, 20] })
    expect(saved.shapes[0].id).not.toBe('old-1')
  })

  it('measures shapes on the project scale and saves a ruler calibration (TOOL-8)', async () => {
    // A stateful stand-in for the server: a refetch sees what was saved.
    let project = {
      id: 'p1',
      settings: { keep: true, calibration: { units_per_pixel: 0.1, unit: 'mm' } },
    } as unknown as Project
    vi.mocked(api.getProject).mockImplementation(async () => project)
    vi.mocked(api.updateProject).mockImplementation(async (_id, payload) => {
      project = { ...project, settings: payload.settings ?? {} }
      return project
    })
    const user = userEvent.setup()

    renderAt('/projects/p1/annotate/i1')
    await waitFor(() => expect(screen.getByTestId('annotator')).toBeInTheDocument())
    expect(await screen.findByText('0.1000 mm per pixel (project)')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'stub-draw' }))
    expect(screen.getByText('0.9 × 0.9 mm')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'stub-calibrate' }))
    expect(screen.getByText('0.5000 mm per pixel (this session)')).toBeInTheDocument()
    expect(screen.getByText('4.5 × 4.5 mm')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Save as project scale' }))
    await waitFor(() =>
      expect(api.updateProject).toHaveBeenCalledWith('p1', {
        settings: { keep: true, calibration: { units_per_pixel: 0.5, unit: 'mm' } },
      }),
    )
    expect(await screen.findByText('Saved as the project scale.')).toBeInTheDocument()
    expect(screen.getByText('0.5000 mm per pixel (project)')).toBeInTheDocument()
  })

  it('opens a text item in the text annotator', async () => {
    vi.mocked(api.getItem).mockResolvedValue(
      makeItem({ media_type: 'text', path: 'docs/a.txt' }),
    )
    vi.mocked(api.fetchMediaText).mockResolvedValue('Alice works at Contoso.')

    renderAt('/projects/p1/annotate/i1')

    await waitFor(() => expect(screen.getByTestId('text-body')).toBeInTheDocument())
    expect(screen.getByTestId('text-body')).toHaveTextContent('Alice works at Contoso.')
    expect(screen.queryByTestId('annotator')).not.toBeInTheDocument()
  })

  it('opens a video item in the video annotator', async () => {
    vi.mocked(api.getItem).mockResolvedValue(
      makeItem({ media_type: 'video', path: 'clips/a.mp4', meta: { fps: 30 } }),
    )

    renderAt('/projects/p1/annotate/i1')

    await waitFor(() => expect(screen.getByTestId('video-overlay')).toBeInTheDocument())
    expect(screen.queryByText(/not supported yet/)).not.toBeInTheDocument()
    expect(screen.queryByTestId('annotator')).not.toBeInTheDocument()
  })

  it('marks a consensus task as blind', async () => {
    vi.mocked(api.nextTask).mockResolvedValue(makeTask({ slot: 1 }))

    renderAt('/projects/p1/annotate')

    await waitFor(() => expect(screen.getByTestId('work-note')).toHaveTextContent(/Consensus/))
  })

  it('warns about shapes outside a region task before saving', async () => {
    vi.mocked(api.nextTask).mockResolvedValue(makeTask({ region: [50, 50, 100, 100] }))
    const user = userEvent.setup()

    renderAt('/projects/p1/annotate')
    await waitFor(() => expect(screen.getByTestId('annotator')).toBeInTheDocument())
    expect(screen.getByTestId('work-note')).toHaveTextContent(/Region task/)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    // The stub draws a box at (1, 1)-(10, 10): outside [50, 50, 100, 100].
    await user.click(screen.getByRole('button', { name: 'stub-draw' }))
    expect(screen.getByRole('alert')).toHaveTextContent('1 shape lies outside')
  })

  it('passes a tile pyramid and a signer for tiled items (IMG-1)', async () => {
    vi.mocked(api.getItem).mockResolvedValue(
      makeItem({
        width: null,
        height: null,
        meta: {
          tiles: {
            format: 'dzi',
            path: 'cache/tiles/i1/',
            tile_size: 256,
            overlap: 0,
            suffix: 'jpeg',
            max_level: 17,
            width: 100000,
            height: 80000,
          },
        },
      }),
    )
    vi.mocked(api.signTiles).mockResolvedValue({ urls: ['https://signed/0/0_0'], expires_in: 900 })
    const user = userEvent.setup()

    renderAt('/projects/p1/annotate/i1')

    const annotator = await screen.findByTestId('annotator')
    expect(annotator).toHaveAttribute('data-tiles', '17')
    await user.click(screen.getByRole('button', { name: 'stub-sign' }))
    expect(api.signTiles).toHaveBeenCalledWith('i1', [[0, 0, 0]])
  })

  it('passes no pyramid for ordinary images', async () => {
    renderAt('/projects/p1/annotate/i1')
    expect(await screen.findByTestId('annotator')).toHaveAttribute('data-tiles', 'none')
  })
})
