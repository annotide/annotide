import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { LicenceServerSection } from './LicenceServerSection'
import { api, ApiError } from '@/api/client'
import type { LicenseRefreshStatus, TelemetryPreview } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      getLicense: vi.fn(() => new Promise(() => {})),
      getLicenseRefresh: vi.fn(),
      refreshLicenseNow: vi.fn(),
      getTelemetryPreview: vi.fn(),
    },
  }
})

const REFRESH: LicenseRefreshStatus = {
  enabled: true,
  server_configured: true,
  payload: { license_id: 'lic-1', install_id: 'acme-prod', host: 'annotate.acme.com' },
  attempted_at: null,
  succeeded_at: null,
  error: null,
  last_payload: null,
}

const TELEMETRY: TelemetryPreview = {
  enabled: false,
  install_id: 'acme-prod',
  version: '0.1.0',
  licence_type: 'commercial',
  active_users: 4,
  fingerprint: { signals: [] },
  withheld: [],
  notice: 'Telemetry is disabled; nothing is sent.',
  server_configured: true,
  attempted_at: null,
  sent_at: null,
  error: null,
  last_payload: null,
}

function renderSection() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <LicenceServerSection />
    </QueryClientProvider>,
  )
}

describe('LicenceServerSection', () => {
  afterEach(() => {
    vi.mocked(api.getLicenseRefresh).mockReset()
    vi.mocked(api.refreshLicenseNow).mockReset()
    vi.mocked(api.getTelemetryPreview).mockReset()
  })

  it('shows what the refresh sends and refreshes on request', async () => {
    vi.mocked(api.getLicenseRefresh).mockResolvedValue(REFRESH)
    vi.mocked(api.getTelemetryPreview).mockResolvedValue(TELEMETRY)
    vi.mocked(api.refreshLicenseNow).mockResolvedValue({
      ...REFRESH,
      attempted_at: '2026-09-25T08:00:00Z',
      error: 'The licence server answered HTTP 503.',
      last_payload: REFRESH.payload,
    })
    renderSection()

    expect(await screen.findByText(/On — once a day/)).toBeInTheDocument()
    expect(screen.getByText(/"host": "annotate.acme.com"/)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Refresh now' }))

    expect(api.refreshLicenseNow).toHaveBeenCalledOnce()
    expect(
      await screen.findByText('Last refresh failed: The licence server answered HTTP 503.'),
    ).toBeInTheDocument()
  })

  it('explains why the refresh is idle and disables the button', async () => {
    vi.mocked(api.getLicenseRefresh).mockResolvedValue({ ...REFRESH, server_configured: false })
    vi.mocked(api.getTelemetryPreview).mockResolvedValue(TELEMETRY)
    renderSection()

    expect(await screen.findByText(/No licence server is configured/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Refresh now' })).toBeDisabled()
  })

  it('reports a refresh that cannot run', async () => {
    vi.mocked(api.getLicenseRefresh).mockResolvedValue(REFRESH)
    vi.mocked(api.getTelemetryPreview).mockResolvedValue(TELEMETRY)
    vi.mocked(api.refreshLicenseNow).mockRejectedValue(
      new ApiError({
        type: 'about:blank',
        title: 'Conflicting state',
        status: 409,
        detail: 'No licence server is configured.',
      }),
    )
    renderSection()

    await userEvent.click(await screen.findByRole('button', { name: 'Refresh now' }))
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('No licence server is configured.'),
    )
  })

  it('lists what was withheld apart from the payload', async () => {
    vi.mocked(api.getLicenseRefresh).mockResolvedValue(REFRESH)
    vi.mocked(api.getTelemetryPreview).mockResolvedValue({
      ...TELEMETRY,
      withheld: ['public hostname: annotide.acme.corp is not routable'],
    })
    renderSection()

    expect(
      await screen.findByText('public hostname: annotide.acme.corp is not routable'),
    ).toBeInTheDocument()
    expect(screen.getByText(/kept on this installation, not sent/)).toBeInTheDocument()
    expect(screen.queryByText(/"withheld"/)).not.toBeInTheDocument()
  })

  it('shows the heartbeat payload and when it was last sent', async () => {
    vi.mocked(api.getLicenseRefresh).mockResolvedValue(REFRESH)
    vi.mocked(api.getTelemetryPreview).mockResolvedValue({
      ...TELEMETRY,
      enabled: true,
      notice: null,
      sent_at: '2026-09-20T08:00:00Z',
      last_payload: { install_id: 'acme-prod' },
    })
    renderSection()

    const heading = await screen.findByText('Heartbeat')
    expect(heading).toBeInTheDocument()
    expect(screen.getByText(/Last sent .*; last tried never\./)).toBeInTheDocument()
    expect(screen.getByText(/"licence_type": "commercial"/)).toBeInTheDocument()
  })
})
