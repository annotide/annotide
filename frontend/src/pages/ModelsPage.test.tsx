import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ModelsPage } from './ModelsPage'
import { api } from '@/api/client'
import { useAuthStore } from '@/lib/store'
import type { Model, ModelVersion, Page, User } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      listModels: vi.fn(),
      createModel: vi.fn(),
      updateModel: vi.fn(),
      deleteModel: vi.fn(),
      checkModel: vi.fn(),
      listModelVersions: vi.fn(),
      getCorrectionMetrics: vi.fn(),
      createModelVersion: vi.fn(),
      listMlPlatforms: vi.fn(),
      getModelFamily: vi.fn(),
    },
  }
})

function makeUser(overrides: Partial<User> = {}): User {
  return {
    id: 'u1',
    organization_id: 'org1',
    email: 'admin@example.com',
    display_name: 'Admin',
    is_active: true,
    is_superuser: true,
    last_seen_at: null,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
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

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/models']}>
        <ModelsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ModelsPage', () => {
  beforeEach(() => {
    vi.mocked(api.listMlPlatforms).mockResolvedValue({ items: [], next_cursor: null })
    useAuthStore.getState().logout()
  })

  afterEach(() => {
    vi.mocked(api.listModels).mockReset()
    vi.mocked(api.createModel).mockReset()
    vi.mocked(api.updateModel).mockReset()
    vi.mocked(api.deleteModel).mockReset()
    vi.mocked(api.checkModel).mockReset()
    vi.mocked(api.listModelVersions).mockReset()
    vi.mocked(api.getCorrectionMetrics).mockReset()
    vi.mocked(api.createModelVersion).mockReset()
    useAuthStore.getState().logout()
  })

  it('requires a secret ref for api_key identity, then posts the payload once provided', async () => {
    useAuthStore.getState().login('token', makeUser())
    vi.mocked(api.listModels).mockResolvedValue(emptyPage<Model>())

    renderPage()
    const user = userEvent.setup()

    await user.type(screen.getByLabelText('Name'), 'Detector')
    await user.type(screen.getByLabelText('Endpoint URL'), 'https://model.example.com')
    await user.selectOptions(screen.getByLabelText('Identity'), 'api_key')
    await user.click(screen.getByRole('button', { name: 'Register model' }))

    expect(
      await screen.findByText('Secret reference is required for this identity type.'),
    ).toBeInTheDocument()
    expect(api.createModel).not.toHaveBeenCalled()

    await user.type(screen.getByLabelText('Secret reference'), 'env://MODEL_API_KEY')
    await user.click(screen.getByRole('button', { name: 'Register model' }))

    await waitFor(() => {
      expect(api.createModel).toHaveBeenCalledWith({
        name: 'Detector',
        task: 'detect',
        endpoint_url: 'https://model.example.com',
        identity_type: 'api_key',
        secret_ref: 'env://MODEL_API_KEY',
      })
    })
  })

  it('registers a managed identity with a token scope and no secret (BYOM-3)', async () => {
    useAuthStore.getState().login('token', makeUser())
    vi.mocked(api.listModels).mockResolvedValue(emptyPage<Model>())

    renderPage()
    const user = userEvent.setup()

    await user.type(screen.getByLabelText('Name'), 'Azure ML')
    await user.type(screen.getByLabelText('Endpoint URL'), 'https://aml.example.com')
    await user.selectOptions(screen.getByLabelText('Identity'), 'managed_identity')
    expect(screen.queryByLabelText('Secret reference')).not.toBeInTheDocument()

    await user.type(screen.getByLabelText('Token scope'), 'https://ml.azure.com')
    await user.click(screen.getByRole('button', { name: 'Register model' }))
    expect(
      await screen.findByText('Token scope is required and must end with /.default.'),
    ).toBeInTheDocument()
    expect(api.createModel).not.toHaveBeenCalled()

    await user.type(screen.getByLabelText('Token scope'), '/.default')
    await user.click(screen.getByRole('button', { name: 'Register model' }))

    await waitFor(() => {
      expect(api.createModel).toHaveBeenCalledWith({
        name: 'Azure ML',
        task: 'detect',
        endpoint_url: 'https://aml.example.com',
        identity_type: 'managed_identity',
        secret_ref: undefined,
        identity_config: { scope: 'https://ml.azure.com/.default', client_id: null },
      })
    })
  })

  it('switching an api_key model to a managed identity drops its secret', async () => {
    useAuthStore.getState().login('token', makeUser())
    vi.mocked(api.listModels).mockResolvedValue({
      items: [makeModel({ identity_type: 'api_key', has_secret: true })],
      next_cursor: null,
    })
    vi.mocked(api.updateModel).mockResolvedValue(makeModel())

    renderPage()
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Edit' }))
    const form = screen.getByRole('form', { name: 'Edit Detector' })
    await user.selectOptions(within(form).getByLabelText('Identity'), 'managed_identity')
    await user.type(
      within(form).getByLabelText('Token scope'),
      'https://cognitiveservices.azure.com/.default',
    )
    await user.type(
      within(form).getByLabelText('User-assigned identity client ID (optional)'),
      'uami-client',
    )
    await user.click(within(form).getByRole('button', { name: 'Save changes' }))

    await waitFor(() => {
      expect(api.updateModel).toHaveBeenCalledWith('m1', {
        identity_type: 'managed_identity',
        secret_ref: null,
        identity_config: {
          scope: 'https://cognitiveservices.azure.com/.default',
          client_id: 'uami-client',
        },
      })
    })
  })

  it('registers an agent model with no endpoint and labels it external (API-8)', async () => {
    useAuthStore.getState().login('token', makeUser())
    vi.mocked(api.listModels).mockResolvedValue({
      items: [makeModel({ id: 'm2', name: 'Claude agent', endpoint_url: null })],
      next_cursor: null,
    })
    vi.mocked(api.createModel).mockResolvedValue(makeModel({ endpoint_url: null }))

    renderPage()
    const user = userEvent.setup()

    expect(await screen.findByText('External (posts its own pre-labels)')).toBeInTheDocument()
    await user.type(screen.getByLabelText('Name'), 'Agent')
    await user.click(screen.getByRole('button', { name: 'Register model' }))

    await waitFor(() => {
      expect(api.createModel).toHaveBeenCalledWith(
        expect.objectContaining({ name: 'Agent', endpoint_url: null }),
      )
    })
  })

  it('adds a version with a class mapping built from the editor rows', async () => {
    useAuthStore.getState().login('token', makeUser())
    vi.mocked(api.listModels).mockResolvedValue({ items: [makeModel()], next_cursor: null })
    vi.mocked(api.listModelVersions).mockResolvedValue(emptyPage<ModelVersion>())
    vi.mocked(api.createModelVersion).mockResolvedValue(makeModelVersion())

    renderPage()
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Versions' }))
    await user.click(await screen.findByRole('button', { name: 'Add version' }))

    await user.click(screen.getByRole('button', { name: 'Add row' }))

    const modelClassInputs = screen.getAllByLabelText('Model class')
    const platformClassInputs = screen.getAllByLabelText('Platform class')
    const dropCheckboxes = screen.getAllByLabelText('Drop')

    await user.type(modelClassInputs[0], 'car')
    await user.type(platformClassInputs[0], 'vehicle')
    await user.type(modelClassInputs[1], 'person')
    await user.click(dropCheckboxes[1])

    await user.click(screen.getByRole('button', { name: 'Add version' }))

    await waitFor(() => {
      expect(api.createModelVersion).toHaveBeenCalledWith('m1', {
        class_mapping: { car: 'vehicle', person: null },
      })
    })
  })

  it('records what a version was trained on and rejects a malformed digest (EXP-8)', async () => {
    useAuthStore.getState().login('token', makeUser())
    vi.mocked(api.listModels).mockResolvedValue({ items: [makeModel()], next_cursor: null })
    vi.mocked(api.listModelVersions).mockResolvedValue(emptyPage<ModelVersion>())
    vi.mocked(api.createModelVersion).mockResolvedValue(makeModelVersion())

    renderPage()
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Versions' }))
    await user.click(await screen.findByRole('button', { name: 'Add version' }))

    await user.type(screen.getByLabelText('Snapshot id'), 'snap-1')
    await user.type(screen.getByLabelText('Snapshot digest (sha256)'), 'nope')
    await user.click(screen.getByRole('button', { name: 'Add version' }))
    expect(
      screen.getByText('Snapshot digest must be 64 hex characters (sha256).'),
    ).toBeInTheDocument()
    expect(api.createModelVersion).not.toHaveBeenCalled()

    await user.clear(screen.getByLabelText('Snapshot digest (sha256)'))
    await user.type(screen.getByLabelText('Snapshot digest (sha256)'), 'AB'.repeat(32))
    await user.type(screen.getByLabelText('Training run (JSON)'), '{{"id": "run-7"}')
    await user.click(screen.getByRole('button', { name: 'Add version' }))

    await waitFor(() => {
      expect(api.createModelVersion).toHaveBeenCalledWith('m1', {
        snapshot_id: 'snap-1',
        snapshot_digest: 'ab'.repeat(32),
        training_run: { id: 'run-7' },
      })
    })
  })

  it('shows the lineage line on a version trained on a snapshot', async () => {
    useAuthStore.getState().login('token', makeUser({ is_superuser: false }))
    vi.mocked(api.listModels).mockResolvedValue({ items: [makeModel()], next_cursor: null })
    vi.mocked(api.listModelVersions).mockResolvedValue({
      items: [
        makeModelVersion({
          snapshot_id: '0123456789abcdef',
          snapshot_digest: 'ab'.repeat(32),
          training_run: { id: 'run-7', url: 'https://ci.example.com/run/7' },
        }),
      ],
      next_cursor: null,
    })

    renderPage()
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Versions' }))

    const link = await screen.findByRole('link', {
      name: 'Trained on snapshot 01234567 · digest abababababab · run run-7',
    })
    expect(link).toHaveAttribute('href', 'https://ci.example.com/run/7')
  })

  it('says how a derived version was made and opens the model family (EXP-8)', async () => {
    useAuthStore.getState().login('token', makeUser({ is_superuser: false }))
    vi.mocked(api.listModels).mockResolvedValue({ items: [makeModel()], next_cursor: null })
    const base = makeModelVersion()
    const small = makeModelVersion({
      id: 'mv2',
      version: 2,
      parent_version_id: 'mv1',
      derivation: 'quantized',
    })
    const distilled = makeModelVersion({
      id: 'mv3',
      version: 3,
      parent_version_id: 'elsewhere',
      derivation: 'distilled',
    })
    vi.mocked(api.listModelVersions).mockResolvedValue({
      items: [base, small, distilled],
      next_cursor: null,
    })
    vi.mocked(api.getModelFamily).mockResolvedValue({
      versions: [base, small].map((v) => ({ ...v, model_name: 'Detector', model_task: 'detect' })),
    })

    renderPage()
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Versions' }))

    expect(await screen.findByText('Quantized from v1')).toBeInTheDocument()
    expect(screen.getByText('Distilled from a version of another model')).toBeInTheDocument()
    expect(api.getModelFamily).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Show family' }))
    expect(
      await screen.findByRole('group', { name: /derivation graph/i }),
    ).toBeInTheDocument()
    expect(api.getModelFamily).toHaveBeenCalledWith('m1')
  })

  it('runs a check and shows the resulting messages', async () => {
    useAuthStore.getState().login('token', makeUser())
    vi.mocked(api.listModels).mockResolvedValue({ items: [makeModel()], next_cursor: null })
    vi.mocked(api.checkModel).mockResolvedValue({
      ok: false,
      messages: ['endpoint unreachable'],
    })

    renderPage()
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Check' }))

    await waitFor(() => {
      expect(api.checkModel).toHaveBeenCalledWith('m1')
    })
    expect(await screen.findByText(/Failed/)).toHaveTextContent('endpoint unreachable')
  })

  it('deletes a model only after confirming', async () => {
    useAuthStore.getState().login('token', makeUser())
    vi.mocked(api.listModels).mockResolvedValue({ items: [makeModel()], next_cursor: null })
    vi.mocked(api.deleteModel).mockResolvedValue(undefined)

    renderPage()
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Delete' }))
    expect(api.deleteModel).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Confirm delete' }))

    await waitFor(() => {
      expect(api.deleteModel).toHaveBeenCalledWith('m1')
    })
  })

  it('shows no write controls for a non-superuser, but still allows expanding versions', async () => {
    useAuthStore.getState().login('token', makeUser({ is_superuser: false }))
    vi.mocked(api.listModels).mockResolvedValue({ items: [makeModel()], next_cursor: null })
    vi.mocked(api.listModelVersions).mockResolvedValue({
      items: [makeModelVersion({ class_mapping: { car: 'vehicle', person: null } })],
      next_cursor: null,
    })

    renderPage()
    const user = userEvent.setup()

    expect(await screen.findByText('Detector')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Check' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Name')).not.toBeInTheDocument()
    expect(
      screen.getByText('Model registration is for system administrators.', { exact: false }),
    ).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Versions' }))

    expect(await screen.findByText('car → vehicle', { exact: false })).toBeInTheDocument()
    expect(await screen.findByText('person → dropped', { exact: false })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add version' })).not.toBeInTheDocument()
  })

  it('fetches correction metrics for a version on demand (ML-5)', async () => {
    useAuthStore.getState().login('token', makeUser({ is_superuser: false }))
    vi.mocked(api.listModels).mockResolvedValue({ items: [makeModel()], next_cursor: null })
    vi.mocked(api.listModelVersions).mockResolvedValue({
      items: [makeModelVersion()],
      next_cursor: null,
    })
    vi.mocked(api.getCorrectionMetrics).mockResolvedValue({
      model_version_id: 'mv1',
      project_id: null,
      items_predicted: 10,
      items_corrected: 8,
      items_pending: 2,
      items_accepted_unchanged: 5,
      shapes: { model: 20, kept: 14, adjusted: 3, relabeled: 1, deleted: 2, added: 4 },
      precision: 0.85,
      recall: 0.7727,
      mean_iou_adjusted: 0.6123,
      classes: [
        {
          name: 'car',
          model: 20,
          kept: 14,
          adjusted: 3,
          relabeled: 1,
          deleted: 2,
          added: 4,
          precision: 0.85,
          recall: 0.7727,
          mean_iou_adjusted: 0.6123,
        },
      ],
    })

    renderPage()
    const user = userEvent.setup()

    await screen.findByText('Detector')
    await user.click(screen.getByRole('button', { name: 'Versions' }))
    await screen.findByText(/^v1/)
    expect(api.getCorrectionMetrics).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Show corrections' }))

    const region = await screen.findByRole('region', { name: 'Corrections for version mv1' })
    expect(api.getCorrectionMetrics).toHaveBeenCalledWith('m1', 'mv1', undefined)
    expect(region).toHaveTextContent('10 items pre-labelled, 8 finished by a human')
    expect(region).toHaveTextContent('Precision 85 % · Recall 77 %')
    expect(region).toHaveTextContent('mean IoU of adjusted boxes 0.61')
    expect(region).toHaveTextContent('14 kept, 3 adjusted, 1 relabeled, 2 deleted, 4 added')
  })
})
