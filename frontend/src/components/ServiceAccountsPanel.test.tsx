import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ServiceAccountsPanel } from './ServiceAccountsPanel'
import { api, ApiError } from '@/api/client'
import type { ApiKey, ApiKeyCreated, User } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      listServiceAccounts: vi.fn(),
      createServiceAccount: vi.fn(),
      deleteServiceAccount: vi.fn(),
      listApiKeys: vi.fn(),
      createApiKey: vi.fn(),
      revokeApiKey: vi.fn(),
    },
  }
})

function makeAccount(overrides: Partial<User> = {}): User {
  return {
    id: 'svc1',
    organization_id: 'org1',
    email: 'svc-abc123@service.invalid',
    display_name: 'Nightly export',
    is_active: true,
    is_superuser: false,
    is_service: true,
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
    user_id: 'svc1',
    name: 'CI',
    scopes: ['read'],
    expires_at: null,
    last_used_at: null,
    revoked_at: null,
    created_by: 'admin',
    created_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <ServiceAccountsPanel />
    </QueryClientProvider>,
  )
}

describe('ServiceAccountsPanel', () => {
  afterEach(() => {
    vi.mocked(api.listServiceAccounts).mockReset()
    vi.mocked(api.createServiceAccount).mockReset()
    vi.mocked(api.deleteServiceAccount).mockReset()
    vi.mocked(api.listApiKeys).mockReset()
    vi.mocked(api.createApiKey).mockReset()
    vi.mocked(api.revokeApiKey).mockReset()
  })

  it('shows an empty state without service accounts', async () => {
    vi.mocked(api.listServiceAccounts).mockResolvedValue([])
    renderPanel()
    expect(await screen.findByText('No service accounts')).toBeInTheDocument()
  })

  it('lists accounts with their e-mail and marks deactivated ones', async () => {
    vi.mocked(api.listServiceAccounts).mockResolvedValue([
      makeAccount(),
      makeAccount({ id: 'svc2', display_name: 'Old sync', is_active: false }),
    ])
    renderPanel()

    const active = await screen.findByRole('listitem', { name: 'Nightly export' })
    expect(within(active).getByText('svc-abc123@service.invalid')).toBeInTheDocument()
    expect(
      within(active).getByRole('button', { name: 'Deactivate Nightly export' }),
    ).toBeInTheDocument()

    const old = screen.getByRole('listitem', { name: 'Old sync' })
    expect(within(old).getByText('deactivated')).toBeInTheDocument()
    expect(within(old).queryByRole('button', { name: /Deactivate/ })).not.toBeInTheDocument()
  })

  it('creates a service account from a display name', async () => {
    vi.mocked(api.listServiceAccounts).mockResolvedValue([])
    vi.mocked(api.createServiceAccount).mockResolvedValue(makeAccount())
    renderPanel()
    const user = userEvent.setup()

    await screen.findByText('No service accounts')
    await user.type(screen.getByLabelText('Display name'), '  Nightly export ')
    await user.click(screen.getByRole('button', { name: 'Create service account' }))

    await waitFor(() =>
      expect(api.createServiceAccount).toHaveBeenCalledWith({ display_name: 'Nightly export' }),
    )
    await waitFor(() => expect(screen.getByLabelText('Display name')).toHaveValue(''))
  })

  it('shows the error when creation fails', async () => {
    vi.mocked(api.listServiceAccounts).mockResolvedValue([])
    vi.mocked(api.createServiceAccount).mockRejectedValue(
      new ApiError({
        type: 'about:blank',
        title: 'Forbidden',
        status: 403,
        detail: 'Superuser only.',
      }),
    )
    renderPanel()
    const user = userEvent.setup()

    await screen.findByText('No service accounts')
    await user.type(screen.getByLabelText('Display name'), 'X')
    await user.click(screen.getByRole('button', { name: 'Create service account' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Superuser only.')
  })

  it('deactivates only after a confirming second click', async () => {
    vi.mocked(api.listServiceAccounts).mockResolvedValue([makeAccount()])
    vi.mocked(api.deleteServiceAccount).mockResolvedValue(undefined)
    renderPanel()
    const user = userEvent.setup()

    const button = await screen.findByRole('button', { name: 'Deactivate Nightly export' })
    await user.click(button)
    expect(api.deleteServiceAccount).not.toHaveBeenCalled()
    expect(screen.getByText(/revokes every key this account holds/)).toBeInTheDocument()

    await user.click(button)
    await waitFor(() => expect(api.deleteServiceAccount).toHaveBeenCalledWith('svc1'))
  })

  it('lists the account keys and mints one for the account', async () => {
    vi.mocked(api.listServiceAccounts).mockResolvedValue([makeAccount()])
    vi.mocked(api.listApiKeys).mockResolvedValue([makeKey()])
    const created: ApiKeyCreated = { ...makeKey({ id: 'k2', name: 'Export' }), token: 'svc_token' }
    vi.mocked(api.createApiKey).mockResolvedValue(created)
    renderPanel()
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Show keys for Nightly export' }))
    expect(await screen.findByRole('button', { name: 'Revoke CI' })).toBeInTheDocument()
    expect(api.listApiKeys).toHaveBeenCalledWith('svc1')

    const form = screen.getByRole('form', { name: 'Create key for Nightly export' })
    await user.type(within(form).getByLabelText('Name'), 'Export')
    await user.clear(within(form).getByLabelText('Expires on'))
    await user.click(within(form).getByRole('button', { name: 'Create key' }))

    await waitFor(() =>
      expect(api.createApiKey).toHaveBeenCalledWith({
        name: 'Export',
        scopes: ['read'],
        expires_at: null,
        user_id: 'svc1',
      }),
    )
    expect(await screen.findByLabelText('API key token')).toHaveTextContent('svc_token')
  })

  it('offers no key form for a deactivated account', async () => {
    vi.mocked(api.listServiceAccounts).mockResolvedValue([makeAccount({ is_active: false })])
    vi.mocked(api.listApiKeys).mockResolvedValue([
      makeKey({ revoked_at: '2024-02-01T00:00:00Z' }),
    ])
    renderPanel()
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Show keys for Nightly export' }))
    expect(await screen.findByText('revoked')).toBeInTheDocument()
    expect(screen.queryByRole('form', { name: /Create key for/ })).not.toBeInTheDocument()
  })
})
