import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { WorkflowSettings } from './WorkflowSettings'
import { api } from '@/api/client'
import { DEFAULT_WORKFLOW } from '@/api/types'
import type { Project, WorkflowConfig } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: { ...actual.api, updateProject: vi.fn() },
  }
})

function makeProject(workflow: WorkflowConfig): Project {
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
    workflow,
    settings: {},
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
  }
}

function renderSettings(workflow = DEFAULT_WORKFLOW, readOnly = false) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <WorkflowSettings projectId="p1" workflow={workflow} readOnly={readOnly} />
    </QueryClientProvider>,
  )
}

describe('WorkflowSettings', () => {
  afterEach(() => {
    vi.mocked(api.updateProject).mockReset()
  })

  it('shows the current config and disables Save until something changes', () => {
    renderSettings({ ...DEFAULT_WORKFLOW, review: 'none', allow_skip: false })
    expect(screen.getByLabelText('review')).toHaveValue('none')
    expect(screen.getByLabelText(/skip items/)).not.toBeChecked()
    // Rejection routing and self-review are moot without a review step.
    expect(screen.getByLabelText('Rejected items go to')).toBeDisabled()
    expect(screen.getByLabelText(/own submissions/)).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Save workflow' })).toBeDisabled()
  })

  it('PATCHes the whole workflow object with the edits', async () => {
    const saved: WorkflowConfig = {
      review: 'required',
      rejection_returns_to: 'queue',
      allow_skip: true,
      allow_self_review: false,
      consensus_annotators: 1,
      review_sample_rate: 0.1,
      gold_every: null,
    }
    vi.mocked(api.updateProject).mockResolvedValue(makeProject(saved))
    const user = userEvent.setup()
    renderSettings()

    await user.selectOptions(screen.getByLabelText('Rejected items go to'), 'queue')
    await user.click(screen.getByLabelText(/own submissions/))
    await user.click(screen.getByRole('button', { name: 'Save workflow' }))

    await waitFor(() => expect(api.updateProject).toHaveBeenCalledWith('p1', { workflow: saved }))
  })

  it('saves the consensus annotator count', async () => {
    const saved: WorkflowConfig = { ...DEFAULT_WORKFLOW, consensus_annotators: 3 }
    vi.mocked(api.updateProject).mockResolvedValue(makeProject(saved))
    const user = userEvent.setup()
    renderSettings()

    await user.selectOptions(screen.getByLabelText('Annotators per item (consensus)'), '3')
    await user.click(screen.getByRole('button', { name: 'Save workflow' }))

    await waitFor(() => expect(api.updateProject).toHaveBeenCalledWith('p1', { workflow: saved }))
  })

  it('disables consensus when review is off', async () => {
    const user = userEvent.setup()
    renderSettings()
    await user.selectOptions(screen.getByLabelText('review'), 'none')
    expect(screen.getByLabelText('Annotators per item (consensus)')).toBeDisabled()
  })

  it('saves sampled review with its rate and a gold rate', async () => {
    const saved: WorkflowConfig = {
      ...DEFAULT_WORKFLOW,
      review: 'sampled',
      review_sample_rate: 0.25,
      gold_every: 10,
    }
    vi.mocked(api.updateProject).mockResolvedValue(makeProject(saved))
    const user = userEvent.setup()
    renderSettings()

    expect(screen.queryByLabelText('Share of items reviewed')).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('review'), 'sampled')
    await user.selectOptions(screen.getByLabelText('Share of items reviewed'), '0.25')
    await user.selectOptions(screen.getByLabelText('Gold tasks in the queue'), '10')
    // Consensus needs every submission reviewed.
    expect(screen.getByLabelText('Annotators per item (consensus)')).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Save workflow' }))

    await waitFor(() => expect(api.updateProject).toHaveBeenCalledWith('p1', { workflow: saved }))
  })

  it('is read-only for non-owners', () => {
    renderSettings(DEFAULT_WORKFLOW, true)
    expect(screen.getByLabelText('review')).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Save workflow' })).not.toBeInTheDocument()
  })
})
