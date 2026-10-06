import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiKeysPage } from './ApiKeysPage'
import { api, ApiError } from '@/api/client'
import { useAuthStore } from '@/lib/store'
import type { ApiKey, ApiKeyCreated, User } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      listApiKeys: vi.fn(),
      createApiKey: vi.fn(),
      revokeApiKey: vi.fn(),
      listServiceAccounts: vi.fn(),
    },
  }
})

function makeUser(overrides: Partial<User> = {}): User {
  return {
    id: 'u1',
    organization_id: 'org1',
    email: 'anna@example.com',
    display_name: 'Anna',
    is_active: true,
    is_superuser: false,
    last_seen_at: null,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeKey(overrides: Partial<ApiKey> = {}): ApiKey {
  return {
    id: 'k1',
    organization_id: 'org1',
    user_id: 'u1',
    name: 'CI export',
    scopes: ['read'],
    expires_at: null,
    last_used_at: null,
    revoked_at: null,
    created_by: 'u1',
    created_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/settings/api-keys']}>
        <ApiKeysPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ApiKeysPage', () => {
  beforeEach(() => {
    useAuthStore.getState().login('token', makeUser())
  })

  afterEach(() => {
    vi.mocked(api.listApiKeys).mockReset()
    vi.mocked(api.createApiKey).mockReset()
    vi.mocked(api.revokeApiKey).mockReset()
    useAuthStore.getState().logout()
  })

  it('shows an empty state when the user has no keys', async () => {
    vi.mocked(api.listApiKeys).mockResolvedValue([])
    renderPage()
    expect(await screen.findByText('No API keys')).toBeInTheDocument()
    expect(api.listApiKeys).toHaveBeenCalledWith(undefined)
  })

  it('lists keys with their status and hides Revoke on revoked ones', async () => {
    vi.mocked(api.listApiKeys).mockResolvedValue([
      makeKey(),
      makeKey({ id: 'k2', name: 'Old', revoked_at: '2024-02-01T00:00:00Z' }),
      makeKey({ id: 'k3', name: 'Stale', expires_at: '2000-01-01T00:00:00Z' }),
    ])
    renderPage()

    const rows = await screen.findAllByRole('row')
    // header + 3 keys
    expect(rows).toHaveLength(4)
    expect(within(rows[1]).getByText('active')).toBeInTheDocument()
    expect(within(rows[2]).getByText('revoked')).toBeInTheDocument()
    expect(within(rows[3]).getByText('expired')).toBeInTheDocument()

    expect(screen.getByRole('button', { name: 'Revoke CI export' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Revoke Old' })).not.toBeInTheDocument()
  })

  it('creates a key with the chosen scopes and shows the token once', async () => {
    vi.mocked(api.listApiKeys).mockResolvedValue([])
    const created: ApiKeyCreated = { ...makeKey({ scopes: ['read', 'write'] }), token: 'ak_secret' }
    vi.mocked(api.createApiKey).mockResolvedValue(created)
    renderPage()
    const user = userEvent.setup()

    await screen.findByText('No API keys')
    expect(screen.getByRole('button', { name: 'Create key' })).toBeDisabled()

    await user.type(screen.getByLabelText('Name'), 'CI export')
    await user.click(screen.getByLabelText(/Write/))
    await user.clear(screen.getByLabelText('Expires on'))
    await user.click(screen.getByRole('button', { name: 'Create key' }))

    await waitFor(() => expect(api.createApiKey).toHaveBeenCalledTimes(1))
    expect(api.createApiKey).toHaveBeenCalledWith({
      name: 'CI export',
      scopes: ['read', 'write'],
      expires_at: null,
    })

    expect(await screen.findByLabelText('API key token')).toHaveTextContent('ak_secret')
    expect(screen.getByText(/shown only once/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByLabelText('API key token')).not.toBeInTheDocument()
  })

  it('sends an ISO expiry when a date is chosen', async () => {
    vi.mocked(api.listApiKeys).mockResolvedValue([])
    vi.mocked(api.createApiKey).mockResolvedValue({ ...makeKey(), token: 't' })
    renderPage()
    const user = userEvent.setup()

    await screen.findByText('No API keys')
    await user.type(screen.getByLabelText('Name'), 'Short-lived')
    const expires = screen.getByLabelText('Expires on')
    await user.clear(expires)
    await user.type(expires, '2030-06-30')
    await user.click(screen.getByRole('button', { name: 'Create key' }))

    await waitFor(() => expect(api.createApiKey).toHaveBeenCalledTimes(1))
    const payload = vi.mocked(api.createApiKey).mock.calls[0][0]
    expect(payload.expires_at).toMatch(/^2030-06-30T|^2030-07-01T/)
    expect(new Date(payload.expires_at as string).getTime()).toBe(
      new Date('2030-06-30T23:59:59').getTime(),
    )
  })

  it('refuses to submit without a scope', async () => {
    vi.mocked(api.listApiKeys).mockResolvedValue([])
    renderPage()
    const user = userEvent.setup()

    await screen.findByText('No API keys')
    await user.type(screen.getByLabelText('Name'), 'Nope')
    await user.click(screen.getByLabelText(/Read/))
    expect(screen.getByRole('button', { name: 'Create key' })).toBeDisabled()
  })

  it('revokes only after a second click, then refreshes the list', async () => {
    vi.mocked(api.listApiKeys)
      .mockResolvedValueOnce([makeKey()])
      .mockResolvedValue([makeKey({ revoked_at: '2024-03-01T00:00:00Z' })])
    vi.mocked(api.revokeApiKey).mockResolvedValue(undefined)
    renderPage()
    const user = userEvent.setup()

    const revoke = await screen.findByRole('button', { name: 'Revoke CI export' })
    await user.click(revoke)
    expect(api.revokeApiKey).not.toHaveBeenCalled()
    expect(screen.getByText(/Click Revoke again to confirm/)).toBeInTheDocument()

    await user.click(revoke)
    await waitFor(() => expect(api.revokeApiKey).toHaveBeenCalledWith('k1'))
    expect(await screen.findByText('revoked')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Revoke CI export' })).not.toBeInTheDocument()
  })

  it('surfaces API errors from creation', async () => {
    vi.mocked(api.listApiKeys).mockResolvedValue([])
    vi.mocked(api.createApiKey).mockRejectedValue(
      new ApiError({
        type: 'about:blank',
        title: 'Forbidden',
        status: 403,
        detail: 'API keys cannot mint keys.',
      }),
    )
    renderPage()
    const user = userEvent.setup()

    await screen.findByText('No API keys')
    await user.type(screen.getByLabelText('Name'), 'x')
    await user.click(screen.getByRole('button', { name: 'Create key' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('API keys cannot mint keys.')
  })

  it('hides service accounts from a non-superuser', async () => {
    vi.mocked(api.listApiKeys).mockResolvedValue([])
    renderPage()
    await screen.findByText('No API keys')
    expect(screen.queryByRole('heading', { name: 'Service accounts' })).not.toBeInTheDocument()
    expect(api.listServiceAccounts).not.toHaveBeenCalled()
  })

  it('shows service accounts to a superuser', async () => {
    useAuthStore.getState().login('token', makeUser({ is_superuser: true }))
    vi.mocked(api.listApiKeys).mockResolvedValue([])
    vi.mocked(api.listServiceAccounts).mockResolvedValue([])
    renderPage()
    expect(await screen.findByRole('heading', { name: 'Service accounts' })).toBeInTheDocument()
    expect(await screen.findByText('No service accounts')).toBeInTheDocument()
  })
})
