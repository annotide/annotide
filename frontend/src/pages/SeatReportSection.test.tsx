import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SeatReportSection, seatReportCsv } from './SeatReportSection'
import { api } from '@/api/client'
import type { LicenseInfo, SeatReport, User } from '@/api/types'
import { useAuthStore } from '@/lib/store'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, api: { ...actual.api, getSeatReport: vi.fn(), getLicense: vi.fn() } }
})

const REPORT: SeatReport = {
  generated_at: '2026-09-25T08:00:00Z',
  install_id: 'acme-prod',
  license_id: 'lic-1',
  licensee: 'Acme, Oy',
  tier: 'team',
  seats: 2,
  seat_limit: 3,
  start: '2026-02-15',
  end: '2026-03-31',
  peak_active_users: 3,
  peak_overage: 1,
  periods: [
    {
      start: '2026-02-15',
      end: '2026-02-28',
      active_users: 1,
      peak_active_users: 1,
      peak_at: '2026-02-20T12:00:00Z',
      overage: 0,
    },
    {
      start: '2026-03-01',
      end: '2026-03-31',
      active_users: 3,
      peak_active_users: 3,
      peak_at: '2026-03-06T12:00:00Z',
      overage: 1,
    },
  ],
}

function renderSection() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <SeatReportSection />
    </QueryClientProvider>,
  )
}

describe('seatReportCsv', () => {
  it('writes a preamble and one row per month, quoting where needed', () => {
    const lines = seatReportCsv(REPORT).trimEnd().split('\n')
    expect(lines).toContain('# licensee: Acme, Oy')
    expect(lines).toContain('# peak_overage: 1')
    expect(lines.slice(-3)).toEqual([
      'period_start,period_end,active_users,peak_active_users,peak_at,overage',
      '2026-02-15,2026-02-28,1,1,2026-02-20T12:00:00Z,0',
      '2026-03-01,2026-03-31,3,3,2026-03-06T12:00:00Z,1',
    ])
  })

  it('leaves overage empty without a key', () => {
    const personal = {
      ...REPORT,
      seats: null,
      peak_overage: null,
      periods: [{ ...REPORT.periods[0], overage: null, peak_at: null }],
    }
    expect(seatReportCsv(personal).trimEnd().split('\n').at(-1)).toBe(
      '2026-02-15,2026-02-28,1,1,,',
    )
  })
})

describe('SeatReportSection', () => {
  afterEach(() => vi.mocked(api.getSeatReport).mockReset())

  it('shows the monthly peaks and asks again for a chosen range', async () => {
    vi.mocked(api.getSeatReport).mockResolvedValue(REPORT)
    renderSection()

    expect(
      await screen.findByText('Peak: 3 active for 2 seats (overage 1)'),
    ).toBeInTheDocument()
    expect(screen.getAllByRole('row')).toHaveLength(3)
    expect(api.getSeatReport).toHaveBeenCalledWith({
      start: undefined,
      end: undefined,
    })

    fireEvent.change(screen.getByLabelText('From'), {
      target: { value: '2026-01-01' },
    })
    await waitFor(() =>
      expect(api.getSeatReport).toHaveBeenLastCalledWith({
        start: '2026-01-01',
        end: undefined,
      }),
    )
  })

  it('downloads the CSV', async () => {
    vi.mocked(api.getSeatReport).mockResolvedValue(REPORT)
    const createObjectURL = vi.fn(() => 'blob:report')
    const revokeObjectURL = vi.fn()
    vi.stubGlobal(
      'URL',
      Object.assign(URL, { createObjectURL, revokeObjectURL }),
    )
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(() => {})
    renderSection()

    const button = await screen.findByRole('button', { name: 'Download CSV' })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)

    expect(createObjectURL).toHaveBeenCalledOnce()
    expect(click).toHaveBeenCalledOnce()
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:report')
    click.mockRestore()
    vi.unstubAllGlobals()
  })

  it('is locked without the seat_report feature and asks for nothing', async () => {
    const admin = {
      id: 'u1',
      organization_id: 'org1',
      email: 'root@example.com',
      display_name: 'Root',
      is_superuser: true,
    } as User
    useAuthStore.getState().login('token', admin)
    vi.mocked(api.getLicense).mockResolvedValue({ features: [] } as unknown as LicenseInfo)
    try {
      renderSection()
      expect(await screen.findByText(/part of the Business edition/)).toBeInTheDocument()
      expect(screen.getByText('Business')).toBeInTheDocument()
      expect(api.getSeatReport).not.toHaveBeenCalled()
    } finally {
      useAuthStore.getState().logout()
    }
  })
})
