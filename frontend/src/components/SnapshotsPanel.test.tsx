import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SnapshotsPanel, describeSplit } from './SnapshotsPanel'
import { api } from '@/api/client'
import type { Job, MlPlatform, Page, Snapshot, SnapshotDiff } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      listSnapshots: vi.fn(),
      createSnapshot: vi.fn(),
      diffSnapshots: vi.fn(),
      createExport: vi.fn(),
      requestRetrain: vi.fn(),
      snapshotLineage: vi.fn(),
      listJobs: vi.fn(),
      listMlPlatforms: vi.fn(),
      publishSnapshot: vi.fn(),
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

function makeSnapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    id: 's1',
    project_id: 'p1',
    name: 'v1',
    filter: {},
    split: null,
    label_schema_version_id: 'lsv1',
    item_count: 12,
    blob_path: 'snapshots/s1/',
    digest: 'abcdef0123456789abcdef',
    created_by_id: 'u1',
    created_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: 'j1',
    project_id: 'p1',
    type: 'snapshot',
    status: 'queued',
    progress: 0,
    payload: { name: 'v3' },
    result: null,
    error: null,
    attempts: 0,
    started_at: null,
    finished_at: null,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  } as Job
}

function makeDiff(): SnapshotDiff {
  return {
    base: {
      id: 's1',
      name: 'v1',
      item_count: 12,
      digest: 'a',
      created_at: '2024-01-01T00:00:00Z',
      split_counts: null,
    },
    target: {
      id: 's2',
      name: 'v2',
      item_count: 14,
      digest: 'b',
      created_at: '2024-01-02T00:00:00Z',
      split_counts: null,
    },
    items: { added: 3, removed: 1, changed: 2, unchanged: 9, split_moved: 0 },
    added: [],
    removed: [],
    changed: [
      {
        item_id: 'i1',
        path: 'images/dog.jpg',
        from_version: 1,
        to_version: 2,
        shapes: { added: 1, removed: 0, changed: 2 },
      },
    ],
    classes: [{ name: 'car', base: 10, target: 13, delta: 3 }],
    truncated: false,
  }
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <SnapshotsPanel projectId="p1" />
    </QueryClientProvider>,
  )
}

describe('describeSplit', () => {
  it('renders ratios as percentages with the grouping', () => {
    expect(describeSplit(makeSnapshot())).toBeNull()
    expect(
      describeSplit(
        makeSnapshot({ split: { train: 0.8, val: 0.1, test: 0.1, seed: 0, group_by: null } }),
      ),
    ).toBe('80 / 10 / 10')
    expect(
      describeSplit(
        makeSnapshot({ split: { train: 0.5, val: 0.25, test: 0.25, seed: 1, group_by: 'folder' } }),
      ),
    ).toBe('50 / 25 / 25 by folder')
  })
})

describe('SnapshotsPanel', () => {
  beforeEach(() => {
    vi.mocked(api.listJobs).mockResolvedValue(page<Job>([]))
    vi.mocked(api.listMlPlatforms).mockResolvedValue(page<MlPlatform>([]))
  })

  afterEach(() => {
    vi.mocked(api.listSnapshots).mockReset()
    vi.mocked(api.createSnapshot).mockReset()
    vi.mocked(api.diffSnapshots).mockReset()
    vi.mocked(api.createExport).mockReset()
    vi.mocked(api.requestRetrain).mockReset()
    vi.mocked(api.snapshotLineage).mockReset()
    vi.mocked(api.listJobs).mockReset()
    vi.mocked(api.listMlPlatforms).mockReset()
    vi.mocked(api.publishSnapshot).mockReset()
  })

  it('creates a snapshot from the filter with a normalised split', async () => {
    vi.mocked(api.listSnapshots).mockResolvedValue(page<Snapshot>([]))
    vi.mocked(api.createSnapshot).mockResolvedValue(makeJob())
    const user = userEvent.setup()

    renderPanel()

    await screen.findByText('No snapshots yet.')
    await user.type(screen.getByLabelText('Snapshot name'), 'release-1')
    await user.selectOptions(screen.getByLabelText('Item status filter'), ['approved'])
    await user.type(screen.getByLabelText('Path prefix'), 'images/')
    await user.click(screen.getByLabelText('Split into train / val / test'))
    await user.clear(screen.getByLabelText('Train ratio'))
    await user.type(screen.getByLabelText('Train ratio'), '70')
    await user.clear(screen.getByLabelText('Val ratio'))
    await user.type(screen.getByLabelText('Val ratio'), '20')
    await user.selectOptions(screen.getByLabelText('Group by'), 'meta')
    await user.type(screen.getByLabelText('Meta key'), 'patient')
    await user.click(screen.getByRole('button', { name: 'Create snapshot' }))

    await waitFor(() => {
      expect(api.createSnapshot).toHaveBeenCalledWith('p1', {
        name: 'release-1',
        filter: { item_status: ['approved'], path_prefix: 'images/' },
        split: { train: 0.7, val: 0.2, test: 0.1, seed: 0, group_by: 'meta.patient' },
      })
    })
  })

  it('refuses to submit without a name', async () => {
    vi.mocked(api.listSnapshots).mockResolvedValue(page<Snapshot>([]))
    const user = userEvent.setup()

    renderPanel()

    await user.click(await screen.findByRole('button', { name: 'Create snapshot' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('name')
    expect(api.createSnapshot).not.toHaveBeenCalled()
  })

  it('lists snapshots and shows in-flight snapshot jobs', async () => {
    vi.mocked(api.listSnapshots).mockResolvedValue(
      page([
        makeSnapshot(),
        makeSnapshot({
          id: 's2',
          name: 'v2',
          split: { train: 0.8, val: 0.1, test: 0.1, seed: 0, group_by: 'folder' },
        }),
      ]),
    )
    vi.mocked(api.listJobs).mockResolvedValue(page([makeJob({ status: 'running' })]))

    renderPanel()

    const row = await screen.findByTestId('snapshot-row-s2')
    expect(within(row).getByText('80 / 10 / 10 by folder')).toBeInTheDocument()
    expect(within(row).getByLabelText('Split to export for v2')).toBeInTheDocument()
    expect(
      within(screen.getByTestId('snapshot-row-s1')).queryByLabelText('Split to export for v1'),
    ).not.toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Snapshot jobs' })).toHaveTextContent('v3: running')
  })

  it('exports one split of a split snapshot', async () => {
    vi.mocked(api.listSnapshots).mockResolvedValue(
      page([
        makeSnapshot({
          id: 's2',
          name: 'v2',
          split: { train: 0.8, val: 0.1, test: 0.1, seed: 0, group_by: null },
        }),
      ]),
    )
    vi.mocked(api.createExport).mockResolvedValue(makeJob({ type: 'export' }))
    const user = userEvent.setup()

    renderPanel()

    const row = await screen.findByTestId('snapshot-row-s2')
    await user.selectOptions(screen.getByLabelText('Export format'), 'yolo')
    await user.selectOptions(within(row).getByLabelText('Split to export for v2'), 'val')
    await user.click(within(row).getByRole('button', { name: 'Export' }))

    await waitFor(() => {
      expect(api.createExport).toHaveBeenCalledWith('p1', {
        format: 'yolo',
        snapshot_id: 's2',
        split: 'val',
      })
    })
    expect(await screen.findByRole('status')).toHaveTextContent('Export queued')
  })

  it('requests a retrain for a snapshot and reports the delivery count', async () => {
    vi.mocked(api.listSnapshots).mockResolvedValue(page([makeSnapshot()]))
    vi.mocked(api.requestRetrain).mockResolvedValue({
      event: 'retrain.requested',
      deliveries: 2,
      ml_run: null,
    })
    const user = userEvent.setup()

    renderPanel()

    const row = await screen.findByTestId('snapshot-row-s1')
    await user.click(within(row).getByRole('button', { name: 'Retrain' }))

    await waitFor(() => {
      expect(api.requestRetrain).toHaveBeenCalledWith('p1', { snapshot_id: 's1' })
    })
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Retrain requested for v1 — sent to 2 webhooks.',
    )
  })

  it('tells the user when no webhook listens for retrain.requested', async () => {
    vi.mocked(api.listSnapshots).mockResolvedValue(page([makeSnapshot()]))
    vi.mocked(api.requestRetrain).mockResolvedValue({
      event: 'retrain.requested',
      deliveries: 0,
      ml_run: null,
    })
    const user = userEvent.setup()

    renderPanel()

    const row = await screen.findByTestId('snapshot-row-s1')
    await user.click(within(row).getByRole('button', { name: 'Retrain' }))

    expect(await screen.findByRole('status')).toHaveTextContent('No webhook subscribes')
  })

  it('shows the model versions trained on a snapshot when its lineage is expanded', async () => {
    vi.mocked(api.listSnapshots).mockResolvedValue(page([makeSnapshot()]))
    vi.mocked(api.snapshotLineage).mockResolvedValue({
      snapshot: {
        id: 's1',
        name: 'v1',
        digest: 'abcdef0123456789abcdef',
        item_count: 12,
        created_at: '2024-01-01T00:00:00Z',
      },
      versions: [
        {
          id: 'mv1',
          model_id: 'm1',
          model_name: 'detector',
          version: 3,
          snapshot_digest: 'abcdef0123456789abcdef',
          training_run: { id: 'run-7', url: 'https://ci.example.com/run/7' },
          created_at: '2024-01-02T00:00:00Z',
          items_predicted: 4,
        },
      ],
    })
    const user = userEvent.setup()

    renderPanel()

    const row = await screen.findByTestId('snapshot-row-s1')
    expect(api.snapshotLineage).not.toHaveBeenCalled()
    await user.click(within(row).getByRole('button', { name: 'Lineage of v1' }))

    const lineage = await screen.findByTestId('lineage-s1')
    expect(api.snapshotLineage).toHaveBeenCalledWith('p1', 's1')
    expect(lineage).toHaveTextContent('detector v3')
    expect(lineage).toHaveTextContent('predicted 4 items here')
    expect(within(lineage).getByRole('link', { name: 'run-7' })).toHaveAttribute(
      'href',
      'https://ci.example.com/run/7',
    )
  })

  it('explains an empty lineage', async () => {
    vi.mocked(api.listSnapshots).mockResolvedValue(page([makeSnapshot()]))
    vi.mocked(api.snapshotLineage).mockResolvedValue({
      snapshot: {
        id: 's1',
        name: 'v1',
        digest: 'abcdef0123456789abcdef',
        item_count: 12,
        created_at: '2024-01-01T00:00:00Z',
      },
      versions: [],
    })
    const user = userEvent.setup()

    renderPanel()

    const row = await screen.findByTestId('snapshot-row-s1')
    await user.click(within(row).getByRole('button', { name: 'Lineage of v1' }))

    expect(await screen.findByTestId('lineage-s1')).toHaveTextContent(
      'No model version records v1 as its training data yet.',
    )
  })

  it('compares two snapshots once both sides are picked', async () => {
    vi.mocked(api.listSnapshots).mockResolvedValue(
      page([makeSnapshot(), makeSnapshot({ id: 's2', name: 'v2' })]),
    )
    vi.mocked(api.diffSnapshots).mockResolvedValue(makeDiff())
    const user = userEvent.setup()

    renderPanel()

    await screen.findByTestId('snapshot-row-s1')
    await user.click(screen.getByLabelText('Compare from v1'))
    expect(api.diffSnapshots).not.toHaveBeenCalled()
    await user.click(screen.getByLabelText('Compare to v2'))

    const region = await screen.findByRole('region', { name: 'Snapshot comparison' })
    expect(api.diffSnapshots).toHaveBeenCalledWith('p1', 's1', 's2')
    expect(region).toHaveTextContent('3 added, 1 removed, 2 changed, 9 unchanged')
    expect(region).toHaveTextContent('+3')
    await user.click(within(region).getByText(/Changed items/))
    expect(region).toHaveTextContent('images/dog.jpg')
  })

  it('asks for two different snapshots', async () => {
    vi.mocked(api.listSnapshots).mockResolvedValue(page([makeSnapshot()]))
    const user = userEvent.setup()

    renderPanel()

    await screen.findByTestId('snapshot-row-s1')
    await user.click(screen.getByLabelText('Compare from v1'))
    await user.click(screen.getByLabelText('Compare to v1'))

    expect(screen.getByText('Pick two different snapshots to compare.')).toBeInTheDocument()
    expect(api.diffSnapshots).not.toHaveBeenCalled()
  })

  it('publishes a snapshot to the chosen ML platform and links the run', async () => {
    vi.mocked(api.listSnapshots).mockResolvedValue(page([makeSnapshot()]))
    vi.mocked(api.listMlPlatforms).mockResolvedValue(page([makePlatform()]))
    vi.mocked(api.publishSnapshot).mockResolvedValue({
      ml_platform_id: 'ml1',
      experiment_id: '1',
      experiment_name: 'annotation/Cars',
      run_id: 'r1',
      run_url: 'http://mlflow:5000/#/experiments/1/runs/r1',
      created: true,
    })
    const user = userEvent.setup()

    renderPanel()

    const row = await screen.findByTestId('snapshot-row-s1')
    expect(within(row).queryByRole('button', { name: /Publish/ })).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('ML platform'), 'ml1')
    await user.click(within(row).getByRole('button', { name: 'Publish v1 to Local MLflow' }))

    await waitFor(() => {
      expect(api.publishSnapshot).toHaveBeenCalledWith('p1', 's1', { ml_platform_id: 'ml1' })
    })
    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent('Published v1 to annotation/Cars.')
    expect(within(status).getByRole('link', { name: 'Open run' })).toHaveAttribute(
      'href',
      'http://mlflow:5000/#/experiments/1/runs/r1',
    )
  })

  it('starts the Databricks job when the chosen platform has one', async () => {
    vi.mocked(api.listSnapshots).mockResolvedValue(page([makeSnapshot()]))
    vi.mocked(api.listMlPlatforms).mockResolvedValue(
      page([
        makePlatform({ id: 'dbx', name: 'Databricks', kind: 'databricks', config: { job_id: 42 } }),
      ]),
    )
    vi.mocked(api.requestRetrain).mockResolvedValue({
      event: 'retrain.requested',
      deliveries: 0,
      ml_run: { ml_platform_id: 'dbx', run_id: '777', run_url: 'https://adb/jobs/42/runs/777' },
    })
    const user = userEvent.setup()

    renderPanel()

    const row = await screen.findByTestId('snapshot-row-s1')
    await user.selectOptions(screen.getByLabelText('ML platform'), 'dbx')
    await user.click(within(row).getByRole('button', { name: 'Retrain' }))

    await waitFor(() => {
      expect(api.requestRetrain).toHaveBeenCalledWith('p1', {
        snapshot_id: 's1',
        ml_platform_id: 'dbx',
      })
    })
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Retrain job started for v1 (run 777).',
    )
  })
})
