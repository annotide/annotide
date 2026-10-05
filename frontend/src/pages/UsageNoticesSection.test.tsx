import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { UsageNoticesSection } from './UsageNoticesSection'
import { api } from '@/api/client'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, api: { ...actual.api, getUsageNotices: vi.fn() } }
})

function renderSection() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <UsageNoticesSection />
    </QueryClientProvider>,
  )
}

describe('UsageNoticesSection', () => {
  afterEach(() => vi.mocked(api.getUsageNotices).mockReset())

  it('lists each notice with who and why', async () => {
    vi.mocked(api.getUsageNotices).mockResolvedValue({
      window_days: 30,
      notices: [
        {
          kind: 'parallel_sign_ins',
          user_id: 'u1',
          email: 'anna@acme.com',
          display_name: 'Anna',
          detail: 'Signed in from two different networks less than 60 minutes apart on 3 days.',
          count: 3,
        },
      ],
    })
    renderSection()

    expect(await screen.findByText(/Parallel sign-ins — Anna/)).toBeInTheDocument()
    expect(screen.getByText('(anna@acme.com)')).toBeInTheDocument()
    expect(screen.getByText(/on 3 days/)).toBeInTheDocument()
  })

  it('says when there is nothing to report', async () => {
    vi.mocked(api.getUsageNotices).mockResolvedValue({ window_days: 30, notices: [] })
    renderSection()

    expect(await screen.findByText('Nothing to report.')).toBeInTheDocument()
  })
})
