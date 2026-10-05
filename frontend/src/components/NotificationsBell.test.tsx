import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { NotificationsBell } from './NotificationsBell'
import { api } from '@/api/client'
import type { Notification } from '@/api/types'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      listNotifications: vi.fn(),
      unreadCount: vi.fn(),
      markNotificationRead: vi.fn(),
      markAllNotificationsRead: vi.fn(),
    },
  }
})

function makeNotification(overrides: Partial<Notification> = {}): Notification {
  return {
    id: 'n1',
    user_id: 'u1',
    type: 'mention',
    payload: { project_id: 'p1', item_id: 'i1', actor_id: 'u2', excerpt: 'look at this' },
    read_at: null,
    created_at: '2026-09-18T09:00:00Z',
    ...overrides,
  }
}

function LocationProbe(): JSX.Element {
  const location = useLocation()
  return <div data-testid="location">{location.pathname}</div>
}

function renderBell() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/']}>
        <NotificationsBell />
        <Routes>
          <Route path="*" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('NotificationsBell', () => {
  beforeEach(() => {
    vi.mocked(api.unreadCount).mockResolvedValue({ count: 2 })
    vi.mocked(api.listNotifications).mockResolvedValue({
      items: [
        makeNotification(),
        makeNotification({
          id: 'n2',
          type: 'reply',
          payload: { project_id: 'p1', item_id: 'i2', actor_id: 'u3', excerpt: 'me too' },
        }),
        makeNotification({
          id: 'n3',
          type: 'review',
          read_at: '2026-09-18T10:00:00Z',
          payload: {
            project_id: 'p1',
            item_id: 'i3',
            actor_id: 'u4',
            annotation_id: 'a1',
            approve: false,
            comment: 'Box too loose',
          },
        }),
      ],
      next_cursor: null,
    })
    vi.mocked(api.markNotificationRead).mockResolvedValue(makeNotification({ read_at: 'x' }))
    vi.mocked(api.markAllNotificationsRead).mockResolvedValue(undefined)
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('shows the unread badge', async () => {
    renderBell()
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Notifications, 2 unread' })).toHaveTextContent(
        '2',
      ),
    )
  })

  it('says whether it is open, and Escape closes it back onto the bell', async () => {
    const user = userEvent.setup()
    renderBell()
    const bell = screen.getByRole('button', { name: /^Notifications/ })
    expect(bell).toHaveAttribute('aria-expanded', 'false')

    await user.click(bell)
    expect(bell).toHaveAttribute('aria-expanded', 'true')
    await user.click(screen.getByRole('button', { name: 'Mark all read' }))
    await user.keyboard('{Escape}')
    expect(bell).toHaveAttribute('aria-expanded', 'false')
    expect(bell).toHaveFocus()
  })

  it('lists notifications with type-specific text when opened', async () => {
    const user = userEvent.setup()
    renderBell()

    await user.click(screen.getByRole('button', { name: /^Notifications/ }))

    await waitFor(() =>
      expect(screen.getByText('You were mentioned: look at this')).toBeInTheDocument(),
    )
    expect(screen.getByText('Reply to your comment: me too')).toBeInTheDocument()
    expect(screen.getByText('Your annotation was rejected: Box too loose')).toBeInTheDocument()
  })

  it('marks a notification read and navigates to its item', async () => {
    const user = userEvent.setup()
    renderBell()

    await user.click(screen.getByRole('button', { name: /^Notifications/ }))
    await waitFor(() =>
      expect(screen.getByText('Reply to your comment: me too')).toBeInTheDocument(),
    )
    await user.click(screen.getByText('Reply to your comment: me too'))

    await waitFor(() => expect(api.markNotificationRead).toHaveBeenCalledWith('n2'))
    expect(screen.getByTestId('location')).toHaveTextContent('/projects/p1/annotate/i2')
  })

  it('marks everything read', async () => {
    const user = userEvent.setup()
    renderBell()

    await user.click(screen.getByRole('button', { name: /^Notifications/ }))
    await user.click(await screen.findByRole('button', { name: 'Mark all read' }))

    await waitFor(() => expect(api.markAllNotificationsRead).toHaveBeenCalled())
  })
})
