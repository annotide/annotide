import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import type { ModelFamilyVersion } from '@/api/types'
import { ModelFamilyPanel } from './ModelFamilyPanel'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, api: { ...actual.api, getModelFamily: vi.fn() } }
})

function version(
  id: string,
  overrides: Partial<ModelFamilyVersion> = {},
): ModelFamilyVersion {
  return {
    id,
    model_id: 'student-model',
    model_name: 'small',
    model_task: 'detect',
    version: 1,
    class_mapping: {},
    metrics: {},
    snapshot_id: null,
    snapshot_digest: null,
    training_run: null,
    parent_version_id: null,
    derivation: null,
    created_at: '2026-10-01T00:00:00Z',
    ...overrides,
  }
}

const FAMILY: ModelFamilyVersion[] = [
  version('t1', {
    model_id: 'teacher-model',
    model_name: 'big',
    metrics: {
      f1: 0.9,
      size_bytes: 160_000_000,
      latency_ms_p50: 400,
      dtype: 'fp32',
      per_class: { car: { f1: 0.95 }, bus: { f1: 0.85 } },
    },
  }),
  version('s1', {
    parent_version_id: 't1',
    derivation: 'distilled',
    created_at: '2026-10-02T00:00:00Z',
    metrics: {
      f1: 0.86,
      size_bytes: 76_000_000,
      latency_ms_p50: 90,
      dtype: 'fp32',
      per_class: { car: { f1: 0.93 }, bus: { f1: 0.7 } },
    },
  }),
  version('s2', {
    version: 2,
    parent_version_id: 's1',
    derivation: 'quantized',
    created_at: '2026-10-03T00:00:00Z',
    metrics: {
      f1: 0.85,
      size_bytes: 19_800_000,
      latency_ms_p50: 80,
      dtype: 'int8',
      per_class: { car: { f1: 0.93 }, bus: { f1: 0.66 } },
    },
  }),
]

function renderPanel(): void {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <ModelFamilyPanel modelId="student-model" />
    </QueryClientProvider>,
  )
}

describe('ModelFamilyPanel', () => {
  beforeEach(() => {
    vi.mocked(api.getModelFamily).mockReset()
  })

  it('draws the family across models with derivation labels', async () => {
    vi.mocked(api.getModelFamily).mockResolvedValue({ versions: FAMILY })
    renderPanel()

    const graph = await screen.findByRole('group', { name: /derivation graph/i })
    expect(within(graph).getByRole('button', { name: /big v1\. fp32 · 160 MB · F1 0\.90/ }))
      .toBeInTheDocument()
    expect(within(graph).getByRole('button', { name: /small v2\. int8 · 19\.8 MB/ }))
      .toBeInTheDocument()
    expect(within(graph).getByText('distilled')).toBeInTheDocument()
    expect(within(graph).getByText('quantized')).toBeInTheDocument()
    expect(api.getModelFamily).toHaveBeenCalledWith('student-model')
  })

  it('compares the newest derived version with its parent by default', async () => {
    vi.mocked(api.getModelFamily).mockResolvedValue({ versions: FAMILY })
    renderPanel()

    const table = await screen.findByRole('table', { name: /small v2 against small v1/ })
    const rows = within(table).getAllByRole('row')
    // Header, then the biggest loss first: bus lost 0.04, car none.
    expect(within(rows[1]).getByText('bus')).toBeInTheDocument()
    expect(within(rows[1]).getByText('-0.04')).toBeInTheDocument()
    expect(within(rows[2]).getByText('+0.00')).toBeInTheDocument()
  })

  it('switches the comparison when another version is selected', async () => {
    vi.mocked(api.getModelFamily).mockResolvedValue({ versions: FAMILY })
    const user = userEvent.setup()
    renderPanel()

    await user.click(await screen.findByRole('button', { name: /^small v1\./ }))
    const table = screen.getByRole('table', { name: /small v1 against big v1/ })
    expect(within(table).getByText('-0.15')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /^big v1\./ }))
    expect(screen.getByText(/no parent in the family/)).toBeInTheDocument()
  })

  it('plots F1 against size, or latency when asked', async () => {
    vi.mocked(api.getModelFamily).mockResolvedValue({ versions: FAMILY })
    const user = userEvent.setup()
    renderPanel()

    const chart = await screen.findByRole('img', { name: /F1 against size/ })
    expect(within(chart).getByText('small v2')).toBeInTheDocument()
    await user.click(screen.getByRole('radio', { name: 'latency' }))
    expect(screen.getByRole('img', { name: /F1 against latency/ })).toBeInTheDocument()
  })

  it('says what is missing when versions have no metrics', async () => {
    vi.mocked(api.getModelFamily).mockResolvedValue({ versions: [version('bare')] })
    renderPanel()

    expect(await screen.findByText(/No version has both f1/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /small v1\./ })).toHaveTextContent('no metrics')
  })
})
