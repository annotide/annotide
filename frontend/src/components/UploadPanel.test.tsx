import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { UploadPanel, collectDroppedEntries, entryPath } from './UploadPanel'
import { ApiError, api } from '@/api/client'
import type { Job, UploadTarget, UploadUrlsResponse } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      createUploadUrls: vi.fn(),
      putToSignedUrl: vi.fn(),
      scanProject: vi.fn(),
      getJob: vi.fn(),
    },
  }
})

const mocked = vi.mocked(api)

function target(path: string): UploadTarget {
  return {
    path,
    url: `/api/v1/storage/local/c1/${path}?expires=1&sig=x`,
    method: 'PUT',
    headers: { 'Content-Type': 'image/png' },
  }
}

function makeJob(overrides: Partial<Job> = {}): Job {
  return {
    id: 'j1',
    project_id: 'p1',
    type: 'scan_source',
    status: 'queued',
    progress: 0,
    payload: {},
    result: null,
    error: null,
    attempts: 0,
    started_at: null,
    finished_at: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    ...overrides,
  }
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
  const view = render(
    <QueryClientProvider client={queryClient}>
      <UploadPanel projectId="p1" />
    </QueryClientProvider>,
  )
  return { ...view, invalidate }
}

function png(name: string, relativePath?: string): File {
  const file = new File(['\x89PNG'], name, { type: 'image/png' })
  if (relativePath !== undefined) {
    Object.defineProperty(file, 'webkitRelativePath', { value: relativePath })
  }
  return file
}

beforeEach(() => {
  vi.clearAllMocks()
  mocked.scanProject.mockResolvedValue(makeJob())
  mocked.getJob.mockResolvedValue(makeJob())
  mocked.putToSignedUrl.mockResolvedValue(undefined)
})

describe('entryPath', () => {
  it('prefers the folder-relative path and falls back to the file name', () => {
    expect(entryPath(png('a.png'))).toBe('a.png')
    expect(entryPath(png('a.png', 'photos/2024/a.png'))).toBe('photos/2024/a.png')
  })
})

describe('collectDroppedEntries', () => {
  it('uses the flat file list when the browser has no entry API', async () => {
    const files = [png('a.png'), png('b.png')]
    const dataTransfer = {
      items: [],
      files,
    } as unknown as DataTransfer
    const entries = await collectDroppedEntries(dataTransfer)
    expect(entries.map((entry) => entry.path)).toEqual(['a.png', 'b.png'])
  })

  it('walks dropped directories and keeps relative paths', async () => {
    const fileEntry = (name: string) => ({
      isFile: true,
      isDirectory: false,
      name,
      file: (ok: (file: File) => void) => ok(png(name)),
    })
    let batches = [[fileEntry('x.png')], []]
    const dirEntry = {
      isFile: false,
      isDirectory: true,
      name: 'photos',
      createReader: () => ({
        readEntries: (ok: (entries: unknown[]) => void) => {
          const [batch, ...rest] = batches
          batches = rest
          ok(batch ?? [])
        },
      }),
    }
    const dataTransfer = {
      items: [
        { webkitGetAsEntry: () => dirEntry },
        { webkitGetAsEntry: () => fileEntry('top.png') },
      ],
      files: [],
    } as unknown as DataTransfer
    const entries = await collectDroppedEntries(dataTransfer)
    expect(entries.map((entry) => entry.path)).toEqual(['photos/x.png', 'top.png'])
  })
})

describe('UploadPanel', () => {
  it('mints URLs for the picked files, PUTs each one, then queues a scan', async () => {
    const response: UploadUrlsResponse = {
      prefix: 'in/',
      uploads: [target('in/a.png'), target('in/sub/b.png')],
    }
    mocked.createUploadUrls.mockResolvedValue(response)
    const user = userEvent.setup()
    renderPanel()

    const input = screen.getByLabelText('Choose files')
    await user.upload(input, [png('a.png'), png('b.png', 'sub/b.png')])
    expect(screen.getByTestId('upload-selection')).toHaveTextContent('2 files')

    await user.click(screen.getByRole('button', { name: /upload 2/i }))

    await waitFor(() => expect(mocked.scanProject).toHaveBeenCalledWith('p1'))
    expect(mocked.createUploadUrls).toHaveBeenCalledWith('p1', {
      files: [
        { path: 'a.png', content_type: 'image/png', size_bytes: 5 },
        { path: 'sub/b.png', content_type: 'image/png', size_bytes: 5 },
      ],
    })
    expect(mocked.putToSignedUrl).toHaveBeenCalledTimes(2)
    expect(mocked.putToSignedUrl.mock.calls[0][0]).toEqual(target('in/a.png'))
    expect(screen.getByRole('status')).toHaveTextContent('2 uploaded · scan queued')
    // Selection cleared after success.
    expect(screen.queryByTestId('upload-selection')).toBeNull()
  })

  it('reports failed PUTs but still scans when something landed', async () => {
    mocked.createUploadUrls.mockResolvedValue({
      prefix: '',
      uploads: [target('a.png'), target('b.png')],
    })
    mocked.putToSignedUrl
      .mockResolvedValueOnce(undefined)
      .mockRejectedValueOnce(new Error('Upload of b.png failed: 500 Boom'))
    const user = userEvent.setup()
    renderPanel()

    await user.upload(screen.getByLabelText('Choose files'), [png('a.png'), png('b.png')])
    await user.click(screen.getByRole('button', { name: /upload 2/i }))

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('1 uploaded, 1 failed'))
    expect(screen.getByRole('status')).toHaveTextContent('Failed: b.png')
    expect(mocked.scanProject).toHaveBeenCalledTimes(1)
  })

  it('does not scan when every PUT failed', async () => {
    mocked.createUploadUrls.mockResolvedValue({ prefix: '', uploads: [target('a.png')] })
    mocked.putToSignedUrl.mockRejectedValue(new Error('nope'))
    const user = userEvent.setup()
    renderPanel()

    await user.upload(screen.getByLabelText('Choose files'), [png('a.png')])
    await user.click(screen.getByRole('button', { name: /upload 1/i }))

    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('0 uploaded, 1 failed'))
    expect(mocked.scanProject).not.toHaveBeenCalled()
  })

  it('shows the API problem detail when minting URLs is refused', async () => {
    mocked.createUploadUrls.mockRejectedValue(
      new ApiError({
        type: 'about:blank',
        title: 'Conflict',
        status: 409,
        detail: 'The source connector (s3) cannot issue upload URLs.',
      }),
    )
    const user = userEvent.setup()
    renderPanel()

    await user.upload(screen.getByLabelText('Choose files'), [png('a.png')])
    await user.click(screen.getByRole('button', { name: /upload 1/i }))

    expect(
      await screen.findByText('The source connector (s3) cannot issue upload URLs.'),
    ).toBeInTheDocument()
    expect(mocked.putToSignedUrl).not.toHaveBeenCalled()
  })

  it('keeps the upload button disabled until something is picked', () => {
    renderPanel()
    expect(screen.getByRole('button', { name: 'Upload' })).toBeDisabled()
  })

  async function uploadOne() {
    mocked.createUploadUrls.mockResolvedValue({ prefix: '', uploads: [target('a.png')] })
    const user = userEvent.setup()
    const view = renderPanel()
    await user.upload(screen.getByLabelText('Choose files'), [png('a.png')])
    await user.click(screen.getByRole('button', { name: /upload 1/i }))
    return view
  }

  it('refreshes the item grid once the queued scan has finished', async () => {
    mocked.getJob.mockResolvedValue(makeJob({ status: 'succeeded', progress: 100 }))
    const { invalidate } = await uploadOne()

    expect(await screen.findByRole('status')).toHaveTextContent('1 uploaded · scan finished')
    expect(mocked.getJob).toHaveBeenCalledWith('j1')
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['projects', 'p1', 'items'] })
  })

  it('reports a scan that failed', async () => {
    mocked.getJob.mockResolvedValue(makeJob({ status: 'failed', error: 'boom' }))
    await uploadOne()

    expect(await screen.findByRole('status')).toHaveTextContent('1 uploaded · scan failed')
  })
})
