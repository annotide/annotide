import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ImportPanel, parseClassMapping } from './ImportPanel'
import { api } from '@/api/client'
import type { Job, LabelSchemaVersion } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      listJobs: vi.fn(),
      uploadImport: vi.fn(),
      listSchemas: vi.fn(),
    },
  }
})

function makeSchemaVersion(overrides: Partial<LabelSchemaVersion> = {}): LabelSchemaVersion {
  return {
    id: 's1',
    label_schema_id: 'schema-1',
    version: 1,
    definition: {
      version: 1,
      classes: [
        {
          name: 'car',
          display_name: 'Car',
          color: '#ff0000',
          tools: ['bbox'],
          attributes: [
            { name: 'color', type: 'text', required: false },
            { name: 'make', type: 'text', required: false },
          ],
        },
      ],
      classification: [],
    },
    created_at: new Date().toISOString(),
    ...overrides,
  }
}

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: 'j1',
    project_id: 'p1',
    type: 'import',
    status: 'succeeded',
    progress: 100,
    payload: { format: 'voc' },
    result: null,
    error: null,
    attempts: 1,
    started_at: null,
    finished_at: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    ...overrides,
  }
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <ImportPanel projectId="p1" />
    </QueryClientProvider>,
  )
}

describe('parseClassMapping', () => {
  it('accepts empty text, an object, and rejects anything else', () => {
    expect(parseClassMapping('')).toEqual({})
    expect(parseClassMapping('{"automobile": "car", "n": 1}')).toEqual({
      automobile: 'car',
      n: '1',
    })
    expect(parseClassMapping('[1]')).toBeNull()
    expect(parseClassMapping('not json')).toBeNull()
  })
})

describe('ImportPanel', () => {
  beforeEach(() => {
    vi.mocked(api.listJobs).mockResolvedValue({ items: [], next_cursor: null })
    vi.mocked(api.uploadImport).mockResolvedValue(makeJob({ status: 'queued', progress: 0 }))
    vi.mocked(api.listSchemas).mockResolvedValue([makeSchemaVersion()])
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('refuses to submit without a file', async () => {
    renderPanel()
    await userEvent.click(screen.getByRole('button', { name: 'Import' }))
    expect(screen.getByText('Choose a file to import.')).toBeInTheDocument()
    expect(api.uploadImport).not.toHaveBeenCalled()
  })

  it('uploads the chosen file with format, status, dry run and class mapping', async () => {
    renderPanel()
    const file = new File(['<annotation/>'], 'a.xml', { type: 'text/xml' })
    await userEvent.selectOptions(screen.getByLabelText('Import format'), 'voc')
    await userEvent.upload(screen.getByLabelText(/File/), file)
    await userEvent.selectOptions(screen.getByLabelText('Write as'), 'draft')
    await userEvent.click(screen.getByLabelText('Dry run'))
    await userEvent.type(screen.getByLabelText(/Class mapping/), '{{"automobile": "car"}')
    await userEvent.click(screen.getByRole('button', { name: 'Preview' }))

    await waitFor(() => expect(api.uploadImport).toHaveBeenCalledTimes(1))
    expect(api.uploadImport).toHaveBeenCalledWith(
      'p1',
      file,
      {
        format: 'voc',
        status: 'draft',
        dry_run: true,
        class_mapping: { automobile: 'car' },
      },
      expect.any(Function),
    )
  })

  it('shows the counts of a finished import, including dropped attributes', async () => {
    vi.mocked(api.listJobs).mockResolvedValue({
      items: [
        makeJob({
          result: {
            format: 'voc',
            dry_run: true,
            parsed: 3,
            matched: 2,
            unmatched: 1,
            imported: 2,
            errors: 0,
            dropped_shapes: 1,
            dropped_attributes: 4,
            classes: { car: 2, unicorn: 1 },
            attributes: {},
            unmatched_sample: ['c.png'],
            problems: [],
          },
        }),
      ],
      next_cursor: null,
    })
    renderPanel()
    expect(await screen.findByText(/Dry run: 2 imported · 2\/3 matched/)).toBeInTheDocument()
    expect(screen.getByText(/1 shapes dropped/)).toBeInTheDocument()
    expect(screen.getByText(/4 attributes dropped/)).toBeInTheDocument()
    expect(screen.getByText(/car \(2\), unicorn \(1\)/)).toBeInTheDocument()
    expect(screen.getByText(/Unmatched: c.png/)).toBeInTheDocument()
  })

  it('shows an accessible progress bar while uploading and updates it via onProgress', async () => {
    let capturedOnProgress: ((fraction: number) => void) | undefined
    vi.mocked(api.uploadImport).mockImplementation((_projectId, _file, _options, onProgress) => {
      capturedOnProgress = onProgress
      return new Promise(() => {
        // never resolves within the test: keeps the mutation "pending" so
        // the progress bar stays mounted while we assert on it.
      })
    })

    renderPanel()
    const file = new File(['{}'], 'a.json', { type: 'application/json' })
    await userEvent.upload(screen.getByLabelText(/File/), file)
    await userEvent.click(screen.getByRole('button', { name: 'Import' }))

    const progressBar = await screen.findByRole('progressbar', { name: 'Upload progress' })
    expect(progressBar).toHaveAttribute('aria-valuenow', '0')

    act(() => capturedOnProgress?.(0.42))

    await waitFor(() => expect(progressBar).toHaveAttribute('aria-valuenow', '42'))
  })

  it('sends non-identity attribute mapping choices after a dry run reports attributes', async () => {
    vi.mocked(api.listJobs).mockResolvedValue({
      items: [
        makeJob({
          result: {
            format: 'coco',
            dry_run: true,
            parsed: 3,
            matched: 3,
            unmatched: 0,
            imported: 0,
            errors: 0,
            dropped_shapes: 0,
            dropped_attributes: 0,
            classes: { automobile: 3 },
            attributes: { automobile: ['colour', 'model'] },
            unmatched_sample: [],
            problems: [],
          },
        }),
      ],
      next_cursor: null,
    })

    renderPanel()
    const file = new File(['{}'], 'a.json', { type: 'application/json' })
    await userEvent.upload(screen.getByLabelText(/File/), file)
    await userEvent.type(screen.getByLabelText(/Class mapping/), '{{"automobile": "car"}')

    // Wait for the attribute mapping editor, built from the dry-run job above.
    await screen.findByText('automobile → car')
    await userEvent.selectOptions(screen.getByLabelText('colour'), 'color')
    await userEvent.selectOptions(screen.getByLabelText('model'), 'Discard')

    await userEvent.click(screen.getByRole('button', { name: 'Import' }))

    await waitFor(() => expect(api.uploadImport).toHaveBeenCalledTimes(1))
    expect(api.uploadImport).toHaveBeenCalledWith(
      'p1',
      file,
      {
        format: 'coco',
        status: 'submitted',
        dry_run: false,
        class_mapping: { automobile: 'car' },
        attribute_mapping: { car: { colour: 'color', model: null } },
      },
      expect.any(Function),
    )
  })

  it('maps source classes with selects built from the last dry run', async () => {
    vi.mocked(api.listJobs).mockResolvedValue({
      items: [
        makeJob({
          result: {
            format: 'coco',
            dry_run: true,
            parsed: 4,
            matched: 4,
            unmatched: 0,
            imported: 0,
            errors: 0,
            dropped_shapes: 0,
            dropped_attributes: 0,
            classes: { automobile: 3, unicorn: 1 },
            attributes: { automobile: [], unicorn: [] },
            unmatched_sample: [],
            problems: [],
          },
        }),
      ],
      next_cursor: null,
    })

    renderPanel()
    const file = new File(['{}'], 'a.json', { type: 'application/json' })
    await userEvent.upload(screen.getByLabelText(/File/), file)

    const automobile = await screen.findByLabelText(/^automobile/)
    // Neither source class is in the schema until it is mapped.
    expect(screen.getAllByText('not in schema — dropped')).toHaveLength(2)
    await userEvent.selectOptions(automobile, 'car')
    expect(screen.getAllByText('not in schema — dropped')).toHaveLength(1)
    // The JSON view follows the selects.
    expect(screen.getByLabelText('Class mapping JSON')).toHaveValue(
      JSON.stringify({ automobile: 'car' }, null, 2),
    )

    await userEvent.click(screen.getByRole('button', { name: 'Import' }))
    await waitFor(() => expect(api.uploadImport).toHaveBeenCalledTimes(1))
    expect(vi.mocked(api.uploadImport).mock.calls[0][2]).toMatchObject({
      class_mapping: { automobile: 'car' },
    })

    // Back to "(same name)" removes the entry.
    await userEvent.selectOptions(automobile, '(same name)')
    expect(screen.getByLabelText('Class mapping JSON')).toHaveValue('')
  })
})
