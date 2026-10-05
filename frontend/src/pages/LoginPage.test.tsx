import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { LoginPage, ssoErrorMessage } from './LoginPage'
import { api, ApiError } from '@/api/client'
import type { AuthProviders, User } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      authProviders: vi.fn(),
      login: vi.fn(),
      me: vi.fn(),
    },
  }
})

function renderPage(initialEntry = '/login') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <LoginPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const withSso: AuthProviders = {
  local: true,
  oidc: { display_name: 'Acme SSO', login_path: '/auth/oidc/login' },
}

describe('LoginPage', () => {
  afterEach(() => {
    vi.mocked(api.authProviders).mockReset()
  })

  it('shows only the local form when no identity provider is configured', async () => {
    vi.mocked(api.authProviders).mockResolvedValue({ local: true, oidc: null })
    renderPage()
    await waitFor(() => expect(api.authProviders).toHaveBeenCalled())
    expect(screen.getByLabelText('Email')).toBeInTheDocument()
    expect(screen.queryByText(/Sign in with/)).not.toBeInTheDocument()
  })

  it('offers single sign-on as a full-page link when configured', async () => {
    vi.mocked(api.authProviders).mockResolvedValue(withSso)
    renderPage()
    const link = await screen.findByRole('link', {
      name: 'Sign in with Acme SSO',
    })
    expect(link).toHaveAttribute('href', '/api/v1/auth/oidc/login')
    // The local form stays: SSO is additive.
    expect(screen.getByLabelText('Password')).toBeInTheDocument()
  })

  it('carries the requested destination into the SSO link', async () => {
    vi.mocked(api.authProviders).mockResolvedValue(withSso)
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[{ pathname: '/login', state: { from: '/projects/p1' } }]}>
          <LoginPage />
        </MemoryRouter>
      </QueryClientProvider>,
    )
    const link = await screen.findByRole('link', { name: /Sign in with/ })
    expect(link).toHaveAttribute('href', '/api/v1/auth/oidc/login?next=%2Fprojects%2Fp1')
  })

  it('explains an SSO error code from the callback redirect', async () => {
    vi.mocked(api.authProviders).mockResolvedValue(withSso)
    renderPage('/login?error=not_provisioned')
    expect(await screen.findByRole('alert')).toHaveTextContent(/No account exists for you/)
  })

  it('falls back to a generic message for an unknown code', () => {
    expect(ssoErrorMessage('something_new')).toMatch(/Single sign-on failed/)
    expect(ssoErrorMessage('inactive')).toMatch(/deactivated/)
    expect(ssoErrorMessage('seat_limit')).toMatch(/licensed seat/)
  })

  it('asks for an authentication code when the account has MFA on', async () => {
    vi.mocked(api.authProviders).mockResolvedValue({ local: true, oidc: null })
    vi.mocked(api.login)
      .mockRejectedValueOnce(
        new ApiError({
          type: 'urn:problem:mfa-required',
          title: 'Authentication code required',
          status: 401,
          detail: 'Enter the code from your authenticator app.',
        }),
      )
      .mockResolvedValueOnce({
        access_token: 't',
        token_type: 'bearer',
        expires_in: 3600,
        mfa_setup_required: false,
      })
    vi.mocked(api.me).mockResolvedValue({ id: 'u1', email: 'anna@acme.com' } as User)
    renderPage()

    await userEvent.type(screen.getByLabelText('Email'), 'anna@acme.com')
    await userEvent.type(screen.getByLabelText('Password'), 'secret')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    const code = await screen.findByLabelText('Authentication code')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await userEvent.type(code, '123456')
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }))

    await waitFor(() =>
      expect(api.login).toHaveBeenLastCalledWith({
        email: 'anna@acme.com',
        password: 'secret',
        otp: '123456',
      }),
    )
    expect(api.me).toHaveBeenCalledWith('t')
  })
})
