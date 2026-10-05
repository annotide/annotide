import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ImportVersionForm, MlPlatformsPanel, toCreate } from './MlPlatformsPanel'
import { api } from '@/api/client'
import type { MlPlatform, ModelVersion, Page } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      listMlPlatforms: vi.fn(),
      createMlPlatform: vi.fn(),
      deleteMlPlatform: vi.fn(),
      checkMlPlatform: vi.fn(),
      importModelVersion: vi.fn(),
    },
  }
})

function page<T>(items: T[]): Page<T> {
  return { items, next_cursor: null }
}

function makePlatform(overrides: Partial<MlPlatform> = {}): MlPlatform {
  return {
    id: 'ml1',
    organization_id: 'o1',
    name: 'Local MLflow',
    kind: 'mlflow',
    tracking_uri: 'http://mlflow:5000',
    identity_type: 'none',
    has_secret: false,
    config: {},
    created_at: '2026-09-28T10:00:00Z',
    updated_at: '2026-09-28T10:00:00Z',
    ...overrides,
  }
}

function renderWithClient(ui: JSX.Element) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>)
}

const EMPTY = {
  name: 'x',
  kind: 'mlflow' as const,
  tracking_uri: ' http://m:5000 ',
  identity_type: 'none' as const,
  secret_ref: 'env:IGNORED',
  client_id: 'ignored',
  tenant_id: 'ignored',
  job_id: '7',
  ui_url: ' http://localhost:5001 ',
}

describe('toCreate', () => {
  it('sends only what the kind and identity take', () => {
    expect(toCreate(EMPTY)).toEqual({
      name: 'x',
      kind: 'mlflow',
      tracking_uri: 'http://m:5000',
      identity_type: 'none',
      secret_ref: null,
      config: { ui_url: 'http://localhost:5001' },
    })
    expect(toCreate({ ...EMPTY, kind: 'databricks', identity_type: 'service_principal' })).toEqual({
      name: 'x',
      kind: 'databricks',
      tracking_uri: 'http://m:5000',
      identity_type: 'service_principal',
      secret_ref: 'env:IGNORED',
      config: { client_id: 'ignored', job_id: 7 },
    })
    expect(
      toCreate({ ...EMPTY, kind: 'azureml', identity_type: 'service_principal' }).config,
    ).toEqual({ client_id: 'ignored', tenant_id: 'ignored' })
  })
})

describe('MlPlatformsPanel', () => {
  afterEach(() => {
    vi.mocked(api.listMlPlatforms).mockReset()
    vi.mocked(api.createMlPlatform).mockReset()
    vi.mocked(api.checkMlPlatform).mockReset()
    vi.mocked(api.deleteMlPlatform).mockReset()
  })

  it('registers a Databricks platform with a service principal and a job', async () => {
    vi.mocked(api.listMlPlatforms).mockResolvedValue(page<MlPlatform>([]))
    vi.mocked(api.createMlPlatform).mockResolvedValue(makePlatform({ kind: 'databricks' }))
    const user = userEvent.setup()

    renderWithClient(<MlPlatformsPanel />)

    expect(await screen.findByText('No ML platforms yet.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Add platform' }))
    const form = screen.getByRole('form', { name: 'Register an ML platform' })
    await user.type(within(form).getByLabelText('Name'), 'Workspace')
    await user.selectOptions(within(form).getByLabelText('Kind'), 'databricks')
    await user.type(
      within(form).getByLabelText('Tracking URI'),
      'https://adb-1.azuredatabricks.net',
    )
    await user.selectOptions(within(form).getByLabelText('Authentication'), 'service_principal')
    await user.type(within(form).getByLabelText('Secret reference'), 'env:DBX_SECRET')
    await user.type(within(form).getByLabelText('Client ID'), 'app-1')
    await user.type(within(form).getByLabelText('Retrain job ID (optional)'), '42')
    await user.click(within(form).getByRole('button', { name: 'Register' }))

    await waitFor(() => {
      expect(api.createMlPlatform).toHaveBeenCalledWith({
        name: 'Workspace',
        kind: 'databricks',
        tracking_uri: 'https://adb-1.azuredatabricks.net',
        identity_type: 'service_principal',
        secret_ref: 'env:DBX_SECRET',
        config: { client_id: 'app-1', job_id: 42 },
      })
    })
  })

  it('checks a platform and shows the result', async () => {
    vi.mocked(api.listMlPlatforms).mockResolvedValue(page([makePlatform()]))
    vi.mocked(api.checkMlPlatform)
      .mockResolvedValueOnce({
        ok: true,
        messages: [],
        info: { tracking_uri: 'http://mlflow:5000', experiments: ['Default'] },
      })
      .mockResolvedValueOnce({ ok: false, messages: ['mlflow call failed: refused'], info: null })
    const user = userEvent.setup()

    renderWithClient(<MlPlatformsPanel />)

    const row = await screen.findByTestId('ml-platform-row')
    await user.click(within(row).getByRole('button', { name: 'Check' }))
    expect(await within(row).findByRole('status')).toHaveTextContent(
      'Connected. Experiments: Default',
    )
    await user.click(within(row).getByRole('button', { name: 'Check' }))
    expect(await within(row).findByRole('alert')).toHaveTextContent('refused')
  })

  it('deletes after a confirmation', async () => {
    vi.mocked(api.listMlPlatforms).mockResolvedValue(page([makePlatform()]))
    vi.mocked(api.deleteMlPlatform).mockResolvedValue(undefined)
    const user = userEvent.setup()

    renderWithClient(<MlPlatformsPanel />)

    const row = await screen.findByTestId('ml-platform-row')
    await user.click(within(row).getByRole('button', { name: 'Delete' }))
    expect(api.deleteMlPlatform).not.toHaveBeenCalled()
    await user.click(within(row).getByRole('button', { name: 'Confirm delete' }))
    await waitFor(() => {
      expect(api.deleteMlPlatform).toHaveBeenCalledWith('ml1')
    })
  })
})

describe('ImportVersionForm', () => {
  afterEach(() => {
    vi.mocked(api.listMlPlatforms).mockReset()
    vi.mocked(api.importModelVersion).mockReset()
  })

  it('imports a registered model version', async () => {
    vi.mocked(api.listMlPlatforms).mockResolvedValue(page([makePlatform()]))
    vi.mocked(api.importModelVersion).mockResolvedValue({} as ModelVersion)
    const onDone = vi.fn()
    const user = userEvent.setup()

    renderWithClient(<ImportVersionForm modelId="m1" onDone={onDone} />)

    const form = await screen.findByRole('form', { name: 'Import a model version from MLflow' })
    await user.selectOptions(within(form).getByLabelText('From'), 'registered')
    await user.type(within(form).getByLabelText('Registered model'), 'detector')
    await user.type(within(form).getByLabelText('Version'), '4')
    await user.click(within(form).getByRole('button', { name: 'Import' }))

    await waitFor(() => {
      expect(api.importModelVersion).toHaveBeenCalledWith('m1', {
        ml_platform_id: 'ml1',
        registered_model: 'detector',
        model_version: '4',
      })
    })
    await waitFor(() => expect(onDone).toHaveBeenCalled())
  })

  it('points at the platform list when there is none', async () => {
    vi.mocked(api.listMlPlatforms).mockResolvedValue(page<MlPlatform>([]))
    renderWithClient(<ImportVersionForm modelId="m1" onDone={vi.fn()} />)
    expect(await screen.findByText('Register an ML platform below first.')).toBeInTheDocument()
  })
})
