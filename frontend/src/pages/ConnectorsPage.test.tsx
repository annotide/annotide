import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ConnectorsPage } from './ConnectorsPage'
import { api } from '@/api/client'
import { useAuthStore } from '@/lib/store'
import type { Connector, Page, User } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      listConnectors: vi.fn(),
      createConnector: vi.fn(),
      updateConnector: vi.fn(),
      deleteConnector: vi.fn(),
      checkConnector: vi.fn(),
      mintConnectorEventToken: vi.fn(),
      revokeConnectorEventToken: vi.fn(),
    },
  }
})

function makeUser(overrides: Partial<User> = {}): User {
  return {
    id: 'u1',
    organization_id: 'org1',
    email: 'admin@example.com',
    display_name: 'Admin',
    is_active: true,
    is_superuser: true,
    last_seen_at: null,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeConnector(overrides: Partial<Connector> = {}): Connector {
  return {
    id: 'c1',
    organization_id: 'org1',
    name: 'Primary blob store',
    type: 'azure_blob',
    identity_type: 'managed_identity',
    has_secret: true,
    events_enabled: false,
    config: { account_url: 'https://acct.blob.core.windows.net', container: 'images' },
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function emptyPage<T>(): Page<T> {
  return { items: [], next_cursor: null }
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/connectors']}>
        <ConnectorsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ConnectorsPage', () => {
  afterEach(() => {
    vi.mocked(api.listConnectors).mockReset()
    vi.mocked(api.createConnector).mockReset()
    vi.mocked(api.updateConnector).mockReset()
    vi.mocked(api.deleteConnector).mockReset()
    vi.mocked(api.checkConnector).mockReset()
    vi.mocked(api.mintConnectorEventToken).mockReset()
    vi.mocked(api.revokeConnectorEventToken).mockReset()
    useAuthStore.getState().logout()
  })

  beforeEach(() => {
    useAuthStore.getState().logout()
  })

  it('lets a superuser create a connector with parsed JSON config', async () => {
    useAuthStore.getState().login('token', makeUser())
    vi.mocked(api.listConnectors).mockResolvedValue(emptyPage<Connector>())
    vi.mocked(api.createConnector).mockResolvedValue(makeConnector())

    renderPage()
    const user = userEvent.setup()

    await user.type(screen.getByLabelText('Name'), 'New store')
    await user.selectOptions(screen.getByLabelText('Type'), 'local')
    await user.selectOptions(screen.getByLabelText('Identity'), 'none')
    const configField = screen.getByLabelText('Config (JSON)')
    await user.clear(configField)
    await user.type(configField, '{{"root": "/data"}')
    await user.click(screen.getByRole('button', { name: 'Create connector' }))

    await waitFor(() => {
      expect(api.createConnector).toHaveBeenCalledWith({
        name: 'New store',
        type: 'local',
        identity_type: 'none',
        secret_ref: undefined,
        config: { root: '/data' },
      })
    })
  })

  it('blocks submit on invalid JSON and calls nothing', async () => {
    useAuthStore.getState().login('token', makeUser())
    vi.mocked(api.listConnectors).mockResolvedValue(emptyPage<Connector>())

    renderPage()
    const user = userEvent.setup()

    await user.type(screen.getByLabelText('Name'), 'Broken store')
    const configField = screen.getByLabelText('Config (JSON)')
    await user.clear(configField)
    await user.type(configField, '{{not json')
    await user.click(screen.getByRole('button', { name: 'Create connector' }))

    expect(await screen.findByText('Config must be valid JSON.')).toBeInTheDocument()
    expect(api.createConnector).not.toHaveBeenCalled()
  })

  it('runs a check and shows the resulting messages', async () => {
    useAuthStore.getState().login('token', makeUser())
    vi.mocked(api.listConnectors).mockResolvedValue({
      items: [makeConnector()],
      next_cursor: null,
    })
    vi.mocked(api.checkConnector).mockResolvedValue({
      ok: false,
      messages: ['container not found'],
    })

    renderPage()
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Check' }))

    await waitFor(() => {
      expect(api.checkConnector).toHaveBeenCalledWith('c1')
    })
    expect(await screen.findByText(/Failed/)).toHaveTextContent('container not found')
  })

  it('edits a connector and sends only the changed fields', async () => {
    useAuthStore.getState().login('token', makeUser())
    vi.mocked(api.listConnectors).mockResolvedValue({
      items: [makeConnector()],
      next_cursor: null,
    })
    vi.mocked(api.updateConnector).mockResolvedValue(makeConnector({ name: 'Renamed store' }))

    renderPage()
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Edit' }))

    const nameField = await screen.findByLabelText('Name')
    await user.clear(nameField)
    await user.type(nameField, 'Renamed store')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))

    await waitFor(() => {
      expect(api.updateConnector).toHaveBeenCalledWith('c1', { name: 'Renamed store' })
    })
  })

  it('deletes a connector only after confirming', async () => {
    useAuthStore.getState().login('token', makeUser())
    vi.mocked(api.listConnectors).mockResolvedValue({
      items: [makeConnector()],
      next_cursor: null,
    })
    vi.mocked(api.deleteConnector).mockResolvedValue(undefined)

    renderPage()
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Delete' }))
    expect(api.deleteConnector).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Confirm delete' }))

    await waitFor(() => {
      expect(api.deleteConnector).toHaveBeenCalledWith('c1')
    })
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'Confirm delete' })).not.toBeInTheDocument()
    })
  })

  it('turns storage events on and shows the event URL once, masked', async () => {
    useAuthStore.getState().login('token', makeUser())
    vi.mocked(api.listConnectors).mockResolvedValue({
      items: [makeConnector()],
      next_cursor: null,
    })
    vi.mocked(api.mintConnectorEventToken).mockResolvedValue({
      token: 'evt_a+b',
      path: '/api/v1/connectors/c1/events',
    })

    renderPage()
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Enable events' }))
    expect(api.mintConnectorEventToken).toHaveBeenCalledWith('c1')

    const url = await screen.findByTestId('connector-event-url')
    expect(url).not.toHaveTextContent('evt_')
    await user.click(screen.getByRole('button', { name: 'Reveal' }))
    expect(url).toHaveTextContent(
      `${window.location.origin}/api/v1/connectors/c1/events?token=evt_a%2Bb`,
    )
    await user.click(screen.getByRole('button', { name: 'Done' }))
    expect(screen.queryByTestId('connector-event-url')).not.toBeInTheDocument()
  })

  it('offers a new event URL and turning events off once they are on', async () => {
    useAuthStore.getState().login('token', makeUser())
    vi.mocked(api.listConnectors).mockResolvedValue({
      items: [makeConnector({ events_enabled: true })],
      next_cursor: null,
    })
    vi.mocked(api.revokeConnectorEventToken).mockResolvedValue(undefined)

    renderPage()
    const user = userEvent.setup()

    expect(await screen.findByText('events on')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New event URL' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Disable events' }))
    await waitFor(() => {
      expect(api.revokeConnectorEventToken).toHaveBeenCalledWith('c1')
    })
  })

  it('shows no write controls for a non-superuser', async () => {
    useAuthStore.getState().login('token', makeUser({ is_superuser: false }))
    vi.mocked(api.listConnectors).mockResolvedValue({
      items: [makeConnector()],
      next_cursor: null,
    })

    renderPage()

    expect(await screen.findByText('Primary blob store')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Check' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Enable events' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Name')).not.toBeInTheDocument()
    expect(
      screen.getByText('Connector registration is for system administrators.', { exact: false }),
    ).toBeInTheDocument()
  })
})
