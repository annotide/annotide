import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ScimPanel } from './ScimPanel'
import { api, ApiError } from '@/api/client'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: { ...actual.api, getScimToken: vi.fn(), mintScimToken: vi.fn(), revokeScimToken: vi.fn() },
  }
})

function renderPanel() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <ScimPanel />
    </QueryClientProvider>,
  )
}

describe('ScimPanel', () => {
  afterEach(() => vi.clearAllMocks())

  it('turns SCIM on and shows the tenant URL and the token once', async () => {
    vi.mocked(api.getScimToken).mockResolvedValue({ enabled: false })
    vi.mocked(api.mintScimToken).mockResolvedValue({
      token: 'scim_secret',
      path: '/api/v1/scim/v2',
    })
    renderPanel()

    expect(await screen.findByText('off')).toBeInTheDocument()
    vi.mocked(api.getScimToken).mockResolvedValue({ enabled: true })
    await userEvent.click(screen.getByRole('button', { name: 'Turn on SCIM' }))

    expect(await screen.findByTestId('scim-base-url')).toHaveTextContent(
      `${window.location.origin}/api/v1/scim/v2`,
    )
    const token = screen.getByTestId('scim-token')
    expect(token).not.toHaveTextContent('scim_secret')
    await userEvent.click(screen.getByRole('button', { name: 'Show token' }))
    expect(token).toHaveTextContent('scim_secret')
    expect(await screen.findByText('on')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Done' }))
    expect(screen.queryByTestId('scim-token')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'New token' })).toBeInTheDocument()
  })

  it('turns SCIM off', async () => {
    vi.mocked(api.getScimToken).mockResolvedValue({ enabled: true })
    vi.mocked(api.revokeScimToken).mockResolvedValue(undefined)
    renderPanel()

    await userEvent.click(await screen.findByRole('button', { name: 'Turn off SCIM' }))
    await waitFor(() => expect(api.revokeScimToken).toHaveBeenCalled())
  })

  it('reports a failure', async () => {
    vi.mocked(api.getScimToken).mockResolvedValue({ enabled: false })
    vi.mocked(api.mintScimToken).mockRejectedValue(
      new ApiError({ type: 'x', title: 'Forbidden', status: 403, detail: 'Nope.' }),
    )
    renderPanel()

    await userEvent.click(await screen.findByRole('button', { name: 'Turn on SCIM' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not change SCIM: Nope.')
  })
})
