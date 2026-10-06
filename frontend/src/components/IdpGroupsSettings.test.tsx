import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { IdpGroupsSettings } from './IdpGroupsSettings'
import { api } from '@/api/client'
import { DEFAULT_WORKFLOW } from '@/api/types'
import type { Project } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: { ...actual.api, updateProject: vi.fn() },
  }
})

function makeProject(settings: Record<string, unknown>): Project {
  return {
    id: 'p1',
    organization_id: 'org1',
    name: 'Demo',
    description: null,
    label_schema_id: null,
    source_connector_id: null,
    result_connector_id: null,
    cache_connector_id: null,
    source_prefix: null,
    source_glob: null,
    workflow: DEFAULT_WORKFLOW,
    settings,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
  }
}

function renderEditor(settings: Record<string, unknown>, readOnly = false) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <IdpGroupsSettings projectId="p1" settings={settings} readOnly={readOnly} />
    </QueryClientProvider>,
  )
}

describe('IdpGroupsSettings', () => {
  afterEach(() => vi.clearAllMocks())

  it('adds a group and saves it merged into the other settings (AUTH-3)', async () => {
    const settings = { calibration: { units_per_pixel: 0.5, unit: 'mm' } }
    vi.mocked(api.updateProject).mockResolvedValue(makeProject(settings))
    renderEditor(settings)

    expect(screen.getByText('No groups mapped.')).toBeInTheDocument()
    const save = screen.getByRole('button', { name: 'Save groups' })
    expect(save).toBeDisabled()

    await userEvent.click(screen.getByRole('button', { name: 'Add group' }))
    await userEvent.type(screen.getByLabelText('group-0'), 'labelers')
    await userEvent.selectOptions(screen.getByLabelText('group-role-0'), 'reviewer')
    await userEvent.click(save)

    await waitFor(() =>
      expect(api.updateProject).toHaveBeenCalledWith('p1', {
        settings: { ...settings, idp_groups: { labelers: 'reviewer' } },
      }),
    )
  })

  it('removes a mapped group', async () => {
    vi.mocked(api.updateProject).mockResolvedValue(makeProject({}))
    renderEditor({ idp_groups: { labelers: 'annotator', leads: 'owner' } })

    expect(screen.getByLabelText('group-1')).toHaveValue('leads')
    await userEvent.click(screen.getAllByRole('button', { name: 'Remove' })[1])
    await userEvent.click(screen.getByRole('button', { name: 'Save groups' }))

    await waitFor(() =>
      expect(api.updateProject).toHaveBeenCalledWith('p1', {
        settings: { idp_groups: { labelers: 'annotator' } },
      }),
    )
  })

  it('is read-only for non-owners', () => {
    renderEditor({ idp_groups: { labelers: 'annotator' } }, true)

    expect(screen.getByLabelText('group-0')).toBeDisabled()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})
