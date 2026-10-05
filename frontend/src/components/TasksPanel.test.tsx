import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TasksPanel, queueOrder } from './TasksPanel'
import { ApiError, api } from '@/api/client'
import type { Item, Member, Page, Task, User } from '@/api/types'
import { useAuthStore } from '@/lib/store'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      listTasks: vi.fn(),
      listMembers: vi.fn(),
      updateTask: vi.fn(),
    },
  }
})

function makeUser(overrides: Partial<User> = {}): User {
  return {
    id: 'u1',
    organization_id: 'org1',
    email: 'owner@example.com',
    display_name: 'Owner',
    is_active: true,
    is_superuser: false,
    last_seen_at: null,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeMember(overrides: Partial<Member> = {}): Member {
  return {
    user_id: 'u1',
    email: 'owner@example.com',
    display_name: 'Owner',
    role: 'owner',
    created_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 't1',
    item_id: 'i1',
    project_id: 'p1',
    type: 'annotate',
    assignee_id: null,
    status: 'open',
    locked_by_id: null,
    locked_until: null,
    priority: 0,
    deadline: null,
    slot: null,
    region: null,
    gold: false,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeItem(overrides: Partial<Item> = {}): Item {
  return {
    id: 'i1',
    project_id: 'p1',
    connector_id: 'c1',
    path: 'images/cat.jpg',
    media_type: 'image',
    etag: null,
    size_bytes: 1,
    width: null,
    height: null,
    duration_ms: null,
    status: 'new',
    meta: {},
    thumbnail_path: null,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  } as Item
}

function page<T>(items: T[]): Page<T> {
  return { items, next_cursor: null }
}

/** Serve `open` and `in_progress` from one list, like the API's status filter. */
function mockTasks(tasks: Task[]): void {
  vi.mocked(api.listTasks).mockImplementation((_projectId, filters) =>
    Promise.resolve(page(tasks.filter((t) => t.status === filters?.status))),
  )
}

function renderPanel(items: Item[] = [makeItem()]) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <TasksPanel projectId="p1" items={items} />
    </QueryClientProvider>,
  )
}

describe('queueOrder', () => {
  it('sorts by priority desc, then deadline asc with nulls last, then created asc', () => {
    const tasks = [
      makeTask({ id: 'old', created_at: '2024-01-01T00:00:00Z' }),
      makeTask({ id: 'new', created_at: '2024-01-02T00:00:00Z' }),
      makeTask({ id: 'due-late', deadline: '2030-02-01T00:00:00Z' }),
      makeTask({ id: 'due-soon', deadline: '2030-01-01T00:00:00Z' }),
      makeTask({ id: 'urgent', priority: 5 }),
    ]
    expect([...tasks].sort(queueOrder).map((t) => t.id)).toEqual([
      'urgent',
      'due-soon',
      'due-late',
      'old',
      'new',
    ])
  })
})

describe('TasksPanel', () => {
  afterEach(() => {
    useAuthStore.getState().logout()
    vi.mocked(api.listTasks).mockReset()
    vi.mocked(api.listMembers).mockReset()
    vi.mocked(api.updateTask).mockReset()
  })

  it('shows an empty state when nothing is queued', async () => {
    mockTasks([])
    vi.mocked(api.listMembers).mockResolvedValue([])

    renderPanel()

    expect(await screen.findByText('Queue is empty')).toBeInTheDocument()
  })

  describe('as an owner', () => {
    beforeEach(() => {
      useAuthStore.getState().login('t', makeUser())
      vi.mocked(api.listMembers).mockResolvedValue([
        makeMember(),
        makeMember({
          user_id: 'u2',
          email: 'ann@example.com',
          display_name: 'Ann',
          role: 'annotator',
        }),
      ])
    })

    it('lists tasks in queue order with item paths and an overdue badge', async () => {
      mockTasks([
        makeTask({ id: 't-low', item_id: 'i1' }),
        makeTask({
          id: 't-high',
          item_id: 'i2',
          priority: 3,
          deadline: '2000-01-01T00:00:00Z',
          status: 'in_progress',
          assignee_id: 'u2',
        }),
      ])

      renderPanel([makeItem(), makeItem({ id: 'i2', path: 'images/dog.jpg' })])

      const rows = await screen.findAllByTestId(/task-row-/)
      expect(rows.map((r) => r.getAttribute('data-testid'))).toEqual([
        'task-row-t-high',
        'task-row-t-low',
      ])
      expect(within(rows[0]).getByText('images/dog.jpg')).toBeInTheDocument()
      expect(within(rows[0]).getByText('overdue')).toBeInTheDocument()
      expect(within(rows[0]).getByText('in progress')).toBeInTheDocument()
      // An in-progress task cannot be reassigned from here.
      expect(within(rows[0]).getByLabelText('Assignee for images/dog.jpg')).toBeDisabled()
      expect(within(rows[1]).getByLabelText('Assignee for images/cat.jpg')).toBeEnabled()
    })

    it('commits a priority change on blur', async () => {
      mockTasks([makeTask()])
      vi.mocked(api.updateTask).mockResolvedValue(makeTask({ priority: 7 }))
      const user = userEvent.setup()

      renderPanel()

      const input = await screen.findByLabelText('Priority for images/cat.jpg')
      await user.clear(input)
      await user.type(input, '7')
      await user.tab()

      await waitFor(() => {
        expect(api.updateTask).toHaveBeenCalledWith('t1', { priority: 7 })
      })
    })

    it('does not call the API when the priority is unchanged', async () => {
      mockTasks([makeTask({ priority: 2 })])
      const user = userEvent.setup()

      renderPanel()

      const input = await screen.findByLabelText('Priority for images/cat.jpg')
      await user.click(input)
      await user.tab()

      expect(api.updateTask).not.toHaveBeenCalled()
    })

    it('reassigns an open task and clears a deadline', async () => {
      mockTasks([makeTask({ deadline: '2030-01-01T00:00:00Z' })])
      vi.mocked(api.updateTask).mockResolvedValue(makeTask())
      const user = userEvent.setup()

      renderPanel()

      await user.selectOptions(await screen.findByLabelText('Assignee for images/cat.jpg'), 'u2')
      await waitFor(() => {
        expect(api.updateTask).toHaveBeenCalledWith('t1', { assignee_id: 'u2' })
      })

      await user.click(screen.getByLabelText('Clear deadline for images/cat.jpg'))
      await waitFor(() => {
        expect(api.updateTask).toHaveBeenCalledWith('t1', { deadline: null })
      })
    })

    it('surfaces a 409 from the server', async () => {
      mockTasks([makeTask()])
      vi.mocked(api.updateTask).mockRejectedValue(
        new ApiError({
          type: 'about:blank',
          status: 409,
          title: 'Conflict',
          detail: 'Task t1 is done and can no longer be changed.',
        }),
      )
      const user = userEvent.setup()

      renderPanel()

      await user.selectOptions(await screen.findByLabelText('Assignee for images/cat.jpg'), 'u2')

      expect(await screen.findByRole('alert')).toHaveTextContent('can no longer be changed')
    })
  })

  describe('as an annotator', () => {
    it('renders read-only values', async () => {
      useAuthStore.getState().login('t', makeUser({ id: 'u2' }))
      vi.mocked(api.listMembers).mockResolvedValue([
        makeMember(),
        makeMember({
          user_id: 'u2',
          email: 'ann@example.com',
          display_name: 'Ann',
          role: 'annotator',
        }),
      ])
      mockTasks([makeTask({ priority: 4, assignee_id: 'u2', deadline: '2030-01-01T00:00:00Z' })])

      renderPanel()

      const row = await screen.findByTestId('task-row-t1')
      expect(within(row).getByText('Ann')).toBeInTheDocument()
      expect(within(row).getByText('4')).toBeInTheDocument()
      expect(within(row).queryByLabelText('Priority for images/cat.jpg')).not.toBeInTheDocument()
      expect(within(row).queryByRole('combobox')).not.toBeInTheDocument()
    })
  })
})
