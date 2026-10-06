import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { LicenseBanner, licenceNotice } from './LicenseBanner'
import { api } from '@/api/client'
import { useAuthStore } from '@/lib/store'
import type { LicenseInfo, User } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, api: { ...actual.api, getLicense: vi.fn() } }
})

function licence(overrides: Partial<LicenseInfo> = {}): LicenseInfo {
  return {
    status: 'valid',
    tier: 'team',
    licensee: 'Acme Oy',
    seats: 10,
    expires_at: '2099-01-01',
    features: [],
    business_features: [],
    owner_only: false,
    trial_used: false,
    license_id: 'lic-1',
    source: 'env',
    active_users: 4,
    seat_limit: 11,
    grace_ends_at: '2099-01-31',
    restricted: false,
    hosts: [],
    host_mismatch: false,
    revoked_at: null,
    ...overrides,
  }
}

function user(isSuperuser: boolean): User {
  return {
    id: 'u1',
    organization_id: 'org1',
    email: 'root@example.com',
    display_name: 'Root',
    is_active: true,
    is_superuser: isSuperuser,
    last_seen_at: null,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
  }
}

function renderBanner() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <LicenseBanner />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('licenceNotice', () => {
  it('says nothing while seats are within the licence', () => {
    expect(licenceNotice(licence({ active_users: 10 }))).toBeNull()
  })

  it('warns about overage without blocking anyone', () => {
    const notice = licenceNotice(licence({ active_users: 11, seat_limit: 12 }))
    expect(notice?.tone).toBe('warning')
    expect(notice?.text).toMatch(/11 active users for 10 seats/)
    expect(notice?.text).toMatch(/invoiced at renewal/)
  })

  it('says new users are refused once the overage is used up', () => {
    const notice = licenceNotice(licence({ active_users: 11 }))
    expect(notice?.tone).toBe('danger')
    expect(notice?.text).toMatch(/New users cannot sign in/)
  })

  it('warns during the grace period and says what stops afterwards', () => {
    const expired = { status: 'expired', expires_at: '2020-06-01', grace_ends_at: '2020-07-01' }
    const grace = licenceNotice(licence({ ...expired, status: 'expired' }))
    expect(grace?.tone).toBe('warning')
    expect(grace?.text).toMatch(/Work continues until/)

    const restricted = licenceNotice(licence({ ...expired, status: 'expired', restricted: true }))
    expect(restricted?.tone).toBe('danger')
    expect(restricted?.text).toMatch(/paused.*reading and export still work/)
  })

  it('names the bound hosts when the install is reached on another one', () => {
    const notice = licenceNotice(
      licence({
        status: 'expired',
        grace_ends_at: '2026-10-25',
        hosts: ['annotate.acme.com'],
        host_mismatch: true,
      }),
    )
    expect(notice?.tone).toBe('warning')
    expect(notice?.text).toMatch(/key is for annotate\.acme\.com, not the address/)
    expect(notice?.text).not.toMatch(/expired on/)
  })

  it('says a licence was revoked rather than expired', () => {
    const notice = licenceNotice(
      licence({ status: 'expired', revoked_at: '2026-09-01', grace_ends_at: '2026-10-01' }),
    )
    expect(notice?.text).toMatch(/^The licence was revoked on .* Work continues until/)
  })

  it('reports an invalid key', () => {
    expect(
      licenceNotice(licence({ status: 'invalid', seats: null, seat_limit: 3, tier: 'community' }))
        ?.text,
    ).toMatch(/not valid, so this installation runs as Community/)
  })

  it('leaves Community and unenforced builds alone', () => {
    const community = { status: 'community', tier: 'community', seats: null } as const
    expect(licenceNotice(licence({ ...community, seat_limit: 3, active_users: 3 }))).toBeNull()
    expect(licenceNotice(licence({ seat_limit: null, active_users: 40 }))).toBeNull()
  })

  it('tells the admin when only the owner can sign in', () => {
    const community = { status: 'community', tier: 'community', seats: null } as const
    const notice = licenceNotice(
      licence({ ...community, seat_limit: 3, active_users: 5, owner_only: true }),
    )
    expect(notice?.tone).toBe('danger')
    expect(notice?.text).toMatch(/only the owner can sign in/)
  })

  it('warns in the last week of a trial, not before', () => {
    const now = new Date('2026-10-02T12:00:00Z')
    const trial = { tier: 'trial', seats: 25, seat_limit: 27, active_users: 4 } as const
    expect(licenceNotice(licence({ ...trial, expires_at: '2026-10-20' }), now)).toBeNull()
    const ending = licenceNotice(licence({ ...trial, expires_at: '2026-10-08' }), now)
    expect(ending?.tone).toBe('warning')
    expect(ending?.text).toMatch(/^The Business trial ends on .*returns to the free Community edition/)
  })
})

describe('LicenseBanner', () => {
  afterEach(() => {
    vi.mocked(api.getLicense).mockReset()
    useAuthStore.getState().logout()
  })

  it('shows the notice to an administrator', async () => {
    useAuthStore.getState().login('token', user(true))
    vi.mocked(api.getLicense).mockResolvedValue(licence({ active_users: 11 }))

    renderBanner()

    expect(await screen.findByRole('status')).toHaveTextContent(/Every seat is in use/)
    expect(screen.getByRole('link', { name: 'Licence settings' })).toHaveAttribute(
      'href',
      '/settings/license',
    )
  })

  it('never asks for the licence for anyone else', () => {
    useAuthStore.getState().login('token', user(false))

    renderBanner()

    expect(api.getLicense).not.toHaveBeenCalled()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})
