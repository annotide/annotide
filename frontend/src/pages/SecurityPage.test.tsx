import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SecurityPage } from './SecurityPage'
import { api, ApiError } from '@/api/client'
import type { MfaStatus, User } from '@/api/types'
import { useAuthStore } from '@/lib/store'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      getMfa: vi.fn(),
      setupMfa: vi.fn(),
      enableMfa: vi.fn(),
      replaceRecoveryCodes: vi.fn(),
      disableMfa: vi.fn(),
      exportPersonalData: vi.fn(),
      me: vi.fn(),
      updateMe: vi.fn(),
    },
  }
})

const OFF: MfaStatus = { enabled: false, pending: false, recovery_codes_left: 0, available: true }
const ON: MfaStatus = { enabled: true, pending: false, recovery_codes_left: 10, available: true }

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <SecurityPage />
    </QueryClientProvider>,
  )
}

const ME: User = {
  id: 'u1',
  organization_id: 'org1',
  email: 'anna@example.com',
  display_name: 'Anna',
  is_active: true,
  is_superuser: false,
  email_notifications: true,
  last_seen_at: null,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
}

describe('SecurityPage', () => {
  beforeEach(() => vi.mocked(api.me).mockResolvedValue(ME))
  afterEach(() => vi.clearAllMocks())

  it('turns notification e-mail off (API-7)', async () => {
    vi.mocked(api.getMfa).mockResolvedValue(OFF)
    vi.mocked(api.updateMe).mockResolvedValue({ ...ME, email_notifications: false })
    renderPage()

    const box = await screen.findByRole('checkbox', { name: 'E-mail me my notifications' })
    expect(box).toBeChecked()
    await userEvent.click(box)

    expect(api.updateMe).toHaveBeenCalledWith({ email_notifications: false })
    await waitFor(() => expect(box).not.toBeChecked())
  })

  it('sets up an authenticator and shows the recovery codes once', async () => {
    vi.mocked(api.getMfa)
      .mockResolvedValueOnce(OFF)
      .mockResolvedValueOnce({ ...OFF, pending: true })
      .mockResolvedValue(ON)
    vi.mocked(api.setupMfa).mockResolvedValue({
      secret: 'JBSWY3DPEHPK3PXP',
      otpauth_uri: 'otpauth://totp/Annotation:anna?secret=JBSWY3DPEHPK3PXP',
    })
    vi.mocked(api.enableMfa).mockResolvedValue({ recovery_codes: ['abcde-fghij', 'klmno-pqrst'] })
    renderPage()

    await userEvent.click(
      await screen.findByRole('button', { name: 'Set up an authenticator app' }),
    )
    expect(await screen.findByText('JBSWY3DPEHPK3PXP')).toBeInTheDocument()
    const qr = await screen.findByRole('img', { name: 'QR code for the authenticator app' })
    expect(qr.getAttribute('src')).toMatch(/^data:image\/png;base64,/)

    await userEvent.type(screen.getByLabelText('Code from the app'), '123456')
    await userEvent.click(screen.getByRole('button', { name: 'Turn on' }))

    expect(api.enableMfa).toHaveBeenCalledWith('123456')
    const list = await screen.findByRole('list', { name: 'Recovery codes' })
    expect(list).toHaveTextContent('abcde-fghij')
    expect(await screen.findByText(/On\. 10 recovery codes left/)).toBeInTheDocument()
  })

  it('shows why a code was refused', async () => {
    vi.mocked(api.getMfa).mockResolvedValue(ON)
    vi.mocked(api.disableMfa).mockRejectedValue(
      new ApiError({
        type: 'urn:problem:mfa-invalid',
        title: 'Invalid authentication code',
        status: 422,
        detail: 'That code does not match or was already used.',
      }),
    )
    renderPage()

    await userEvent.type(await screen.findByLabelText('Code or recovery code'), '000000')
    await userEvent.click(screen.getByRole('button', { name: 'Turn off' }))

    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('does not match or was already used'),
    )
  })

  it('points single sign-on users to their provider', async () => {
    vi.mocked(api.getMfa).mockResolvedValue({ ...OFF, available: false })
    renderPage()

    expect(await screen.findByText(/set up at your identity provider/)).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('downloads the signed-in user\'s personal data as JSON (SEC-6)', async () => {
    vi.mocked(api.getMfa).mockResolvedValue(OFF)
    vi.mocked(api.exportPersonalData).mockResolvedValue({ user: { id: 'u1' } })
    const createObjectURL = vi.fn(() => 'blob:data')
    const revokeObjectURL = vi.fn()
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL, revokeObjectURL }))
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    useAuthStore.getState().login('token', {
      id: 'u1',
      organization_id: 'org1',
      email: 'anna@example.com',
      display_name: 'Anna',
      is_active: true,
      is_superuser: false,
      last_seen_at: null,
      created_at: '2024-01-01T00:00:00Z',
      updated_at: '2024-01-01T00:00:00Z',
    })
    try {
      renderPage()
      await userEvent.click(await screen.findByRole('button', { name: 'Download my data' }))

      await waitFor(() => expect(click).toHaveBeenCalledOnce())
      expect(api.exportPersonalData).toHaveBeenCalledWith('u1')
      expect(revokeObjectURL).toHaveBeenCalledWith('blob:data')
    } finally {
      useAuthStore.getState().logout()
      click.mockRestore()
      vi.unstubAllGlobals()
    }
  })
})
