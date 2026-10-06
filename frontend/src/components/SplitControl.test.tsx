import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api, ApiError } from '@/api/client'
import type { Item } from '@/api/types'

import { SplitControl } from './SplitControl'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, api: { ...actual.api, splitItem: vi.fn() } }
})

function makeItem(overrides: Partial<Item>): Item {
  return {
    id: 'i1',
    project_id: 'p1',
    connector_id: 'c1',
    path: 'a.tif',
    media_type: 'image',
    etag: null,
    size_bytes: 1,
    width: 100,
    height: 80,
    meta: {},
    status: 'new',
    thumbnail_path: null,
    media_url: null,
    thumbnail_url: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  } as Item
}

function renderControl(items: Item[]) {
  const queryClient = new QueryClient()
  return render(
    <QueryClientProvider client={queryClient}>
      <SplitControl projectId="p1" items={items} />
    </QueryClientProvider>,
  )
}

describe('SplitControl', () => {
  afterEach(() => vi.clearAllMocks())

  it('renders nothing without an image in the selection', () => {
    const { container } = renderControl([makeItem({ media_type: 'text' })])
    expect(container).toBeEmptyDOMElement()
  })

  it('splits each selected image and lists the refusals', async () => {
    vi.mocked(api.splitItem)
      .mockResolvedValueOnce({ tasks: [] })
      .mockRejectedValueOnce(
        new ApiError({ type: 'about:blank', title: 'Conflict', status: 409, detail: 'already split' }),
      )
    const user = userEvent.setup()
    renderControl([
      makeItem({ id: 'i1', path: 'a.tif' }),
      makeItem({ id: 'i2', path: 'b.tif' }),
      makeItem({ id: 'i3', path: 'c.txt', media_type: 'text' }),
    ])

    await user.selectOptions(screen.getByLabelText('rows'), '3')
    await user.type(screen.getByLabelText('overlap'), '8')
    await user.click(screen.getByRole('button', { name: 'Split into region tasks' }))

    await waitFor(() => expect(api.splitItem).toHaveBeenCalledTimes(2))
    expect(api.splitItem).toHaveBeenCalledWith('i1', {
      grid: { rows: 3, cols: 2, overlap_px: 8 },
    })
    expect(await screen.findByRole('status')).toHaveTextContent('Split 1 item.')
    expect(screen.getByText('b.tif: already split')).toBeInTheDocument()
  })
})
