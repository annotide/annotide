import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { WebhooksPanel } from './WebhooksPanel'
import { api } from '@/api/client'
import type { Page, Webhook, WebhookDelivery, WebhookWithSecret } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      listWebhooks: vi.fn(),
      createWebhook: vi.fn(),
      updateWebhook: vi.fn(),
      deleteWebhook: vi.fn(),
      testWebhook: vi.fn(),
      listWebhookDeliveries: vi.fn(),
    },
  }
})

function page<T>(items: T[]): Page<T> {
  return { items, next_cursor: null }
}

function makeHook(overrides: Partial<Webhook> = {}): Webhook {
  return {
    id: 'w1',
    organization_id: 'org1',
    project_id: 'p1',
    url: 'https://hooks.example/in',
    description: null,
    events: ['*'],
    is_active: true,
    format: 'json',
    created_by_id: 'u1',
    last_delivery_at: null,
    last_response_status: null,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeDelivery(overrides: Partial<WebhookDelivery> = {}): WebhookDelivery {
  return {
    id: 'd1',
    webhook_id: 'w1',
    event: 'webhook.test',
    payload: {},
    status: 'pending',
    attempts: 0,
    next_attempt_at: '2024-01-01T00:00:00Z',
    response_status: null,
    error: null,
    delivered_at: null,
    created_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderPanel({ projectId }: { projectId?: string } = { projectId: 'p1' }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <WebhooksPanel projectId={projectId} />
    </QueryClientProvider>,
  )
}

describe('WebhooksPanel', () => {
  afterEach(() => {
    vi.mocked(api.listWebhooks).mockReset()
    vi.mocked(api.createWebhook).mockReset()
    vi.mocked(api.updateWebhook).mockReset()
    vi.mocked(api.deleteWebhook).mockReset()
    vi.mocked(api.testWebhook).mockReset()
    vi.mocked(api.listWebhookDeliveries).mockReset()
  })

  it('creates a project hook with chosen events and reveals the secret once', async () => {
    vi.mocked(api.listWebhooks).mockResolvedValue(page<Webhook>([]))
    const created: WebhookWithSecret = { ...makeHook(), secret: 'abc123secret' }
    vi.mocked(api.createWebhook).mockResolvedValue(created)
    const user = userEvent.setup()

    renderPanel()

    await screen.findByText('No webhooks yet.')
    await user.type(screen.getByLabelText('Webhook URL'), 'https://hooks.example/in')
    // Un-tick "all", pick two specific events.
    await user.click(screen.getByRole('checkbox', { name: 'all' }))
    await user.click(screen.getByRole('checkbox', { name: 'annotation.approved' }))
    await user.click(screen.getByRole('checkbox', { name: 'retrain.requested' }))
    await user.click(screen.getByRole('button', { name: 'Add webhook' }))

    await waitFor(() => {
      expect(api.createWebhook).toHaveBeenCalledWith({
        url: 'https://hooks.example/in',
        events: ['annotation.approved', 'retrain.requested'],
        project_id: 'p1',
        description: null,
        format: 'json',
      })
    })
    const secret = await screen.findByTestId('webhook-secret')
    expect(secret).not.toHaveTextContent('abc123secret')
    await user.click(screen.getByRole('button', { name: 'Reveal' }))
    expect(secret).toHaveTextContent('abc123secret')
  })

  it('creates a Slack hook and labels chat hooks in the list', async () => {
    vi.mocked(api.listWebhooks).mockResolvedValue(
      page<Webhook>([makeHook({ id: 'w2', format: 'teams' })]),
    )
    vi.mocked(api.createWebhook).mockResolvedValue({ ...makeHook({ format: 'slack' }), secret: 's' })
    const user = userEvent.setup()

    renderPanel()

    expect(await screen.findByTestId('webhook-row-w2')).toHaveTextContent('Microsoft Teams')
    await user.type(screen.getByLabelText('Webhook URL'), 'https://hooks.slack.com/services/T/B/x')
    await user.selectOptions(screen.getByLabelText('Webhook format'), 'slack')
    await user.click(screen.getByRole('button', { name: 'Add webhook' }))

    await waitFor(() => {
      expect(api.createWebhook).toHaveBeenCalledWith(expect.objectContaining({ format: 'slack' }))
    })
  })

  it('refuses a non-http URL without calling the API', async () => {
    vi.mocked(api.listWebhooks).mockResolvedValue(page<Webhook>([]))
    const user = userEvent.setup()

    renderPanel()

    await screen.findByText('No webhooks yet.')
    await user.type(screen.getByLabelText('Webhook URL'), 'ftp://nope')
    await user.click(screen.getByRole('button', { name: 'Add webhook' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('http(s) URL')
    expect(api.createWebhook).not.toHaveBeenCalled()
  })

  it('pauses, tests (opening the log), rotates and deletes a hook', async () => {
    vi.mocked(api.listWebhooks).mockResolvedValue(
      page([makeHook({ events: ['job.failed'], last_response_status: 500 })]),
    )
    vi.mocked(api.updateWebhook).mockImplementation((id, body) =>
      Promise.resolve({
        ...makeHook({ id, is_active: body.is_active ?? true }),
        secret: body.rotate_secret ? 'rotated-secret' : null,
      }),
    )
    vi.mocked(api.testWebhook).mockResolvedValue(makeDelivery())
    vi.mocked(api.listWebhookDeliveries).mockResolvedValue(
      page([makeDelivery({ status: 'succeeded', attempts: 1, response_status: 200 })]),
    )
    vi.mocked(api.deleteWebhook).mockResolvedValue(undefined)
    const user = userEvent.setup()

    renderPanel()

    const row = await screen.findByTestId('webhook-row-w1')
    expect(row).toHaveTextContent('job.failed')
    expect(row).toHaveTextContent('last response 500')

    await user.click(within(row).getByRole('button', { name: 'Pause' }))
    await waitFor(() => {
      expect(api.updateWebhook).toHaveBeenCalledWith('w1', { is_active: false })
    })

    await user.click(within(row).getByRole('button', { name: 'Send test' }))
    await waitFor(() => expect(api.testWebhook).toHaveBeenCalledWith('w1'))
    const log = await within(row).findByRole('table', { name: 'Deliveries' })
    expect(log).toHaveTextContent('webhook.test')
    expect(log).toHaveTextContent('succeeded')

    await user.click(within(row).getByRole('button', { name: 'Rotate secret' }))
    await waitFor(() => {
      expect(api.updateWebhook).toHaveBeenCalledWith('w1', { rotate_secret: true })
    })
    await user.click(await screen.findByRole('button', { name: 'Reveal' }))
    expect(screen.getByTestId('webhook-secret')).toHaveTextContent('rotated-secret')

    await user.click(within(row).getByRole('button', { name: 'Delete' }))
    expect(api.deleteWebhook).not.toHaveBeenCalled()
    await user.click(within(row).getByRole('button', { name: 'Confirm delete' }))
    await waitFor(() => expect(api.deleteWebhook).toHaveBeenCalledWith('w1'))
  })

  it('lists organisation-wide hooks when no project is given', async () => {
    vi.mocked(api.listWebhooks).mockResolvedValue(page([makeHook({ project_id: null })]))

    renderPanel({})

    await screen.findByTestId('webhook-row-w1')
    expect(api.listWebhooks).toHaveBeenCalledWith(undefined, { limit: 50 })
    expect(screen.getByText(/in the organisation/)).toBeInTheDocument()
  })
})
