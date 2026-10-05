import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { LicensePage } from './LicensePage'
import { api, ApiError } from '@/api/client'
import type { LicenseInfo } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      getLicense: vi.fn(),
      installLicense: vi.fn(),
      removeLicense: vi.fn(),
      startTrial: vi.fn(),
      // The seat report and licence server sections have their own tests.
      getSeatReport: vi.fn(() => new Promise(() => {})),
      getLicenseRefresh: vi.fn(() => new Promise(() => {})),
      getTelemetryPreview: vi.fn(() => new Promise(() => {})),
      getUsageNotices: vi.fn(() => new Promise(() => {})),
    },
  }
})

const ALL_FEATURES: LicenseInfo['business_features'] = [
  'sso',
  'scim',
  'path_permissions',
  'audit_history',
  'seat_report',
  'sharepoint',
  'ml_platforms',
  'quality',
]

const COMMUNITY: LicenseInfo = {
  status: 'community',
  tier: 'community',
  licensee: null,
  seats: null,
  expires_at: null,
  features: [],
  business_features: ALL_FEATURES,
  owner_only: false,
  trial_used: false,
  license_id: null,
  source: null,
  active_users: 1,
  seat_limit: 3,
  grace_ends_at: null,
  restricted: false,
  hosts: [],
  host_mismatch: false,
  revoked_at: null,
}

const TEAM: LicenseInfo = {
  ...COMMUNITY,
  status: 'valid',
  tier: 'team',
  licensee: 'Acme Oy',
  seats: 10,
  expires_at: '2099-01-01',
  license_id: 'lic-1',
  source: 'admin',
  active_users: 4,
  seat_limit: 11,
  grace_ends_at: '2099-01-31',
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/settings/license']}>
        <LicensePage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('LicensePage', () => {
  afterEach(() => {
    vi.mocked(api.getLicense).mockReset()
    vi.mocked(api.installLicense).mockReset()
    vi.mocked(api.removeLicense).mockReset()
    vi.mocked(api.startTrial).mockReset()
  })

  it('shows the Community edition and its three seats', async () => {
    vi.mocked(api.getLicense).mockResolvedValue(COMMUNITY)
    renderPage()

    const details = await screen.findByRole('region', { name: 'Licence details' })
    expect(details).toHaveTextContent('Community')
    expect(details).toHaveTextContent('1 of 3 — Community is for up to three people')
    expect(screen.queryByRole('button', { name: 'Remove stored key' })).not.toBeInTheDocument()
  })

  it('installs a pasted key and shows the new licence', async () => {
    vi.mocked(api.getLicense).mockResolvedValue(COMMUNITY)
    vi.mocked(api.installLicense).mockResolvedValue(TEAM)
    renderPage()

    await userEvent.type(await screen.findByLabelText('Licence key'), 'ANN1.abc.def')
    await userEvent.click(screen.getByRole('button', { name: 'Install key' }))

    expect(api.installLicense).toHaveBeenCalledWith({ key: 'ANN1.abc.def' })
    expect(await screen.findByText('Licence installed.')).toBeInTheDocument()
    const details = screen.getByRole('region', { name: 'Licence details' })
    expect(details).toHaveTextContent('Acme Oy')
    expect(details).toHaveTextContent('4 of 10 (+1 overage allowed)')
    expect(details).toHaveTextContent('Pasted here')
    expect(screen.getByLabelText('Licence key')).toHaveValue('')
  })

  it('explains why a key was refused', async () => {
    vi.mocked(api.getLicense).mockResolvedValue(COMMUNITY)
    vi.mocked(api.installLicense).mockRejectedValue(
      new ApiError({
        type: 'urn:annotation:error:invalid-license-key',
        title: 'Invalid licence key',
        status: 422,
        detail: 'This licence key expired on 2020-01-01.',
      }),
    )
    renderPage()

    await userEvent.type(await screen.findByLabelText('Licence key'), 'ANN1.old.key')
    await userEvent.click(screen.getByRole('button', { name: 'Install key' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('expired on 2020-01-01')
  })

  it('removes the stored key after confirmation', async () => {
    vi.mocked(api.getLicense).mockResolvedValue(TEAM)
    vi.mocked(api.removeLicense).mockResolvedValue(COMMUNITY)
    renderPage()

    await userEvent.click(await screen.findByRole('button', { name: 'Remove stored key' }))
    expect(api.removeLicense).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Remove' }))

    await waitFor(() => expect(api.removeLicense).toHaveBeenCalledTimes(1))
    expect(
      await screen.findByText('1 of 3 — Community is for up to three people'),
    ).toBeInTheDocument()
  })

  it('shows the seat notice from the licence', async () => {
    vi.mocked(api.getLicense).mockResolvedValue({ ...TEAM, active_users: 11 })
    renderPage()

    expect(await screen.findByRole('alert')).toHaveTextContent(/Every seat is in use/)
  })

  it('shows Community usage, the next step and the locked Business features', async () => {
    vi.mocked(api.getLicense).mockResolvedValue({ ...COMMUNITY, active_users: 3 })
    renderPage()

    const edition = await screen.findByRole('region', { name: 'Edition' })
    expect(edition).toHaveTextContent('3 of 3 people active on the free Community edition.')
    expect(edition).toHaveTextContent('Team adds people')
    expect(screen.getByRole('link', { name: 'See pricing' })).toHaveAttribute(
      'href',
      'https://annotide.com/pricing/',
    )
    expect(edition).toHaveTextContent('Single sign-on (Entra ID, OIDC)')
    expect(screen.getAllByText('Business')).toHaveLength(ALL_FEATURES.length)
  })

  it('starts the Business trial and shows what it unlocks', async () => {
    vi.mocked(api.getLicense).mockResolvedValue(COMMUNITY)
    vi.mocked(api.startTrial).mockResolvedValue({
      ...TEAM,
      tier: 'trial',
      source: 'trial',
      seats: 25,
      seat_limit: 27,
      expires_at: '2099-01-01',
      features: ALL_FEATURES,
      trial_used: true,
    })
    renderPage()

    await userEvent.click(await screen.findByRole('button', { name: 'Start 30-day Business trial' }))

    expect(api.startTrial).toHaveBeenCalledTimes(1)
    const edition = await screen.findByRole('region', { name: 'Edition' })
    await waitFor(() => expect(edition).toHaveTextContent('Business trial'))
    expect(edition).toHaveTextContent('Every Business feature, for up to 25 people')
    expect(screen.getAllByText('Included')).toHaveLength(ALL_FEATURES.length)
    expect(screen.queryByRole('button', { name: 'Start 30-day Business trial' })).not.toBeInTheDocument()
  })

  it('explains a refused trial', async () => {
    vi.mocked(api.getLicense).mockResolvedValue(COMMUNITY)
    vi.mocked(api.startTrial).mockRejectedValue(
      new ApiError({
        type: 'urn:annotation:error:trial-unavailable',
        title: 'Trial not available',
        status: 409,
        detail: 'This installation or its host has already had a trial.',
      }),
    )
    renderPage()

    await userEvent.click(await screen.findByRole('button', { name: 'Start 30-day Business trial' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('already had a trial')
  })

  it('offers no second trial', async () => {
    vi.mocked(api.getLicense).mockResolvedValue({ ...COMMUNITY, trial_used: true })
    renderPage()

    expect(await screen.findByText(/has had its Business trial/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Start 30-day Business trial' })).not.toBeInTheDocument()
  })
})
