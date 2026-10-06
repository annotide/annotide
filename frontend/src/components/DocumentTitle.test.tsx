import { render, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { DocumentTitle } from './DocumentTitle'
import { api } from '@/api/client'
import type { Project } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, api: { ...actual.api, getProject: vi.fn() } }
})

function renderAt(path: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <DocumentTitle />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('DocumentTitle (UX-7, WCAG 2.4.2)', () => {
  afterEach(() => {
    vi.mocked(api.getProject).mockReset()
  })

  it.each([
    ['/', 'Projects · Annotide'],
    ['/connectors', 'Connectors · Annotide'],
    ['/settings/license', 'Licence · Annotide'],
    ['/login', 'Sign in · Annotide'],
    ['/no/such/page', 'Page not found · Annotide'],
  ])('names %s', async (path, title) => {
    renderAt(path)
    await waitFor(() => expect(document.title).toBe(title))
  })

  it('puts the page first and the project after it', async () => {
    vi.mocked(api.getProject).mockResolvedValue({ id: 'p1', name: 'Traffic' } as Project)
    renderAt('/projects/p1/annotate/i1')
    await waitFor(() => expect(document.title).toBe('Annotate · Traffic · Annotide'))
    expect(api.getProject).toHaveBeenCalledWith('p1')
  })

  it("names a project's own page after the project", async () => {
    vi.mocked(api.getProject).mockResolvedValue({ id: 'p1', name: 'Traffic' } as Project)
    renderAt('/projects/p1')
    await waitFor(() => expect(document.title).toBe('Traffic · Annotide'))
  })
})
