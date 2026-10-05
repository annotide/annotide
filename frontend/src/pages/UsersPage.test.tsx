import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { UsersPage } from './UsersPage'
import { api, ApiError } from '@/api/client'
import type { User } from '@/api/types'
import { useAuthStore } from '@/lib/store'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      listUsers: vi.fn(),
      eraseUser: vi.fn(),
      getScimToken: vi.fn().mockResolvedValue({ enabled: false }),
    },
  }
})

function person(id: string, email: string, extra: Partial<User> = {}): User {
  return {
    id,
    organization_id: 'org',
    email,
    display_name: email.split('@')[0],
    is_active: true,
    is_superuser: false,
    erased_at: null,
    last_seen_at: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...extra,
  }
}

const ADMIN = person('admin', 'admin@example.com', { is_superuser: true })
const ANNA = person('anna', 'anna@example.com')
const GONE = person('gone', 'erased-gone@erased.invalid', {
  display_name: 'Erased user',
  is_active: false,
  erased_at: '2026-09-01T00:00:00Z',
})

function renderPage(user: User = ADMIN) {
  useAuthStore.getState().login('token', user)
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <UsersPage />
    </QueryClientProvider>,
  )
}

describe('UsersPage', () => {
  afterEach(() => {
    vi.clearAllMocks()
    useAuthStore.getState().logout()
  })

  it('erases a person only after the e-mail is typed', async () => {
    vi.mocked(api.listUsers).mockResolvedValue([ADMIN, ANNA, GONE])
    vi.mocked(api.eraseUser).mockResolvedValue({ ...ANNA, erased_at: '2026-09-28T00:00:00Z' })
    renderPage()

    const row = await screen.findByRole('listitem', { name: 'anna@example.com' })
    // No erase for oneself or for an already erased person.
    expect(
      within(screen.getByRole('listitem', { name: 'admin@example.com' })).queryByRole('button'),
    ).toBeNull()
    expect(
      within(screen.getByRole('listitem', { name: GONE.email })).queryByRole('button'),
    ).toBeNull()
    expect(screen.getByText(/^erased /)).toBeInTheDocument()

    await userEvent.click(within(row).getByRole('button', { name: 'Erase anna@example.com' }))
    const submit = within(row).getByRole('button', { name: 'Erase permanently' })
    expect(submit).toBeDisabled()
    await userEvent.type(within(row).getByLabelText(/to confirm/), 'ANNA@example.com')
    await userEvent.click(within(row).getByLabelText(/replace the text of their comments/))
    expect(submit).toBeEnabled()
    await userEvent.click(submit)

    await waitFor(() =>
      expect(api.eraseUser).toHaveBeenCalledWith('anna', {
        confirm_email: 'ANNA@example.com',
        redact_comments: true,
      }),
    )
    await waitFor(() => expect(api.listUsers).toHaveBeenCalledTimes(2))
  })

  it('shows the server error when erasure fails', async () => {
    vi.mocked(api.listUsers).mockResolvedValue([ADMIN, ANNA])
    vi.mocked(api.eraseUser).mockRejectedValue(
      new ApiError({ status: 409, title: 'Conflict', detail: 'Already erased.', type: 'about:blank' }),
    )
    renderPage()

    const row = await screen.findByRole('listitem', { name: 'anna@example.com' })
    await userEvent.click(within(row).getByRole('button', { name: 'Erase anna@example.com' }))
    await userEvent.type(within(row).getByLabelText(/to confirm/), 'anna@example.com')
    await userEvent.click(within(row).getByRole('button', { name: 'Erase permanently' }))

    expect(await within(row).findByRole('alert')).toHaveTextContent('Already erased.')
  })

  it('passes the search to the API', async () => {
    vi.mocked(api.listUsers).mockResolvedValue([])
    renderPage()

    await userEvent.type(screen.getByLabelText('Search'), 'bob')

    await waitFor(() => expect(api.listUsers).toHaveBeenLastCalledWith('bob'))
    expect(await screen.findByText('No users match')).toBeInTheDocument()
  })

  it('does not load the directory for non-admins', () => {
    renderPage(ANNA)

    expect(screen.getByText(/for system administrators/)).toBeInTheDocument()
    expect(api.listUsers).not.toHaveBeenCalled()
  })
})
