import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/api/client'

import { ViewsPanel } from './ViewsPanel'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, api: { ...actual.api, getItemViews: vi.fn(), fetchMediaText: vi.fn() } }
})

vi.mock('@/features/pdf-annotator', () => ({
  PdfView: ({ url, name }: { url: string; name: string }) => (
    <div data-testid="pdf-view">
      {name} {url}
    </div>
  ),
}))

function renderPanel(meta: Record<string, unknown>): void {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ViewsPanel itemId="i1" meta={meta} />
    </QueryClientProvider>,
  )
}

describe('ViewsPanel', () => {
  afterEach(() => vi.clearAllMocks())

  it('shows text as text, images as images and the rest as links', async () => {
    vi.mocked(api.getItemViews).mockResolvedValue([
      { path: 'set/1.txt', label: 'Caption', media_type: 'text', url: 'https://s/1.txt' },
      { path: 'set/1-map.png', label: null, media_type: 'image', url: 'https://s/1-map.png' },
      { path: 'set/1.bin', label: null, media_type: null, url: 'https://s/1.bin' },
      { path: 'set/1.json', label: null, media_type: 'text', url: null },
    ])
    vi.mocked(api.fetchMediaText).mockResolvedValue('A red bus on a bridge.')
    renderPanel({ views: [{ path: 'set/1.txt' }] })

    expect(await screen.findByRole('heading', { name: 'Context' })).toBeInTheDocument()
    expect(await screen.findByText('A red bus on a bridge.')).toBeInTheDocument()
    expect(screen.getByText('Caption')).toBeInTheDocument()
    expect(screen.getByRole('img', { name: '1-map.png' })).toHaveAttribute('src', 'https://s/1-map.png')
    expect(screen.getByRole('link', { name: 'Open 1.bin' })).toHaveAttribute('href', 'https://s/1.bin')
    expect(screen.getByText('This view cannot be shown right now.')).toBeInTheDocument()
  })

  it('shows a PDF page by page, e.g. the original beside its extracted text', async () => {
    vi.mocked(api.getItemViews).mockResolvedValue([
      { path: 'docs/a.pdf', label: 'PDF', media_type: 'pdf', url: 'https://s/a.pdf' },
    ])
    renderPanel({ views: [{ path: 'docs/a.pdf', label: 'PDF' }] })

    expect(await screen.findByTestId('pdf-view')).toHaveTextContent('PDF https://s/a.pdf')
  })

  it('renders nothing and fetches nothing for an item without views', () => {
    renderPanel({ content_type: 'image/png' })
    expect(screen.queryByRole('heading', { name: 'Context' })).toBeNull()
    expect(api.getItemViews).not.toHaveBeenCalled()
  })
})
