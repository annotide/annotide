import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/api/client'
import type { Task } from '@/api/types'
import { useTaskStore } from '@/lib/store'
import { HEARTBEAT_MS, useTaskQueue } from './useTaskQueue'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      nextTask: vi.fn(),
      releaseTask: vi.fn(),
      extendTask: vi.fn(),
    },
  }
})

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: 't1',
    item_id: 'i1',
    project_id: 'p1',
    type: 'annotate',
    assignee_id: 'u1',
    status: 'in_progress',
    locked_by_id: 'u1',
    locked_until: '2026-09-18T10:00:00Z',
    priority: 0,
    deadline: null,
    slot: null,
    region: null,
    gold: false,
    created_at: '2026-09-18T09:00:00Z',
    updated_at: '2026-09-18T09:00:00Z',
    ...overrides,
  }
}

describe('useTaskQueue', () => {
  beforeEach(() => {
    useTaskStore.getState().clearTask()
    vi.mocked(api.releaseTask).mockResolvedValue(makeTask({ status: 'open' }))
    vi.mocked(api.extendTask).mockResolvedValue(makeTask())
  })

  afterEach(() => {
    vi.mocked(api.nextTask).mockReset()
    vi.mocked(api.releaseTask).mockReset()
    vi.mocked(api.extendTask).mockReset()
    vi.useRealTimers()
  })

  it('claims on entry to queue mode and hands the task to onClaimed', async () => {
    const task = makeTask()
    vi.mocked(api.nextTask).mockResolvedValue(task)
    const onClaimed = vi.fn()

    const { result } = renderHook(() =>
      useTaskQueue({ projectId: 'p1', type: 'annotate', queueMode: true, onClaimed }),
    )

    await waitFor(() => expect(onClaimed).toHaveBeenCalledWith(task))
    expect(api.nextTask).toHaveBeenCalledWith({ project_id: 'p1', type: 'annotate' })
    expect(result.current.task).toEqual(task)
    expect(result.current.empty).toBe(false)
  })

  it('reports an empty queue and can check again', async () => {
    vi.mocked(api.nextTask).mockResolvedValue(null)

    const { result } = renderHook(() =>
      useTaskQueue({ projectId: 'p1', type: 'review', queueMode: true, onClaimed: vi.fn() }),
    )

    await waitFor(() => expect(result.current.empty).toBe(true))
    expect(api.nextTask).toHaveBeenCalledTimes(1)

    act(() => result.current.claimNext())
    await waitFor(() => expect(api.nextTask).toHaveBeenCalledTimes(2))
  })

  it('does not claim outside queue mode', () => {
    renderHook(() =>
      useTaskQueue({ projectId: 'p1', type: 'annotate', queueMode: false, onClaimed: vi.fn() }),
    )
    expect(api.nextTask).not.toHaveBeenCalled()
  })

  it('ignores a second claim while one is in flight', async () => {
    let resolve: (task: Task | null) => void = () => {}
    vi.mocked(api.nextTask).mockReturnValue(
      new Promise<Task | null>((r) => {
        resolve = r
      }),
    )

    const { result } = renderHook(() =>
      useTaskQueue({ projectId: 'p1', type: 'annotate', queueMode: true, onClaimed: vi.fn() }),
    )
    act(() => result.current.claimNext())
    act(() => result.current.claimNext())
    await act(async () => {
      resolve(null)
    })

    expect(api.nextTask).toHaveBeenCalledTimes(1)
  })

  it('heartbeats the lock while a task is held', async () => {
    vi.useFakeTimers()
    useTaskStore.getState().setTask(makeTask())

    renderHook(() =>
      useTaskQueue({ projectId: 'p1', type: 'annotate', queueMode: false, onClaimed: vi.fn() }),
    )

    await act(async () => {
      vi.advanceTimersByTime(HEARTBEAT_MS)
    })
    expect(api.extendTask).toHaveBeenCalledWith('t1')
  })

  it('releases the held task on unmount', async () => {
    useTaskStore.getState().setTask(makeTask())

    const { unmount } = renderHook(() =>
      useTaskQueue({ projectId: 'p1', type: 'annotate', queueMode: false, onClaimed: vi.fn() }),
    )
    unmount()

    await waitFor(() => expect(api.releaseTask).toHaveBeenCalledWith('t1'))
    expect(useTaskStore.getState().task).toBeNull()
  })

  it('finish forgets the task without releasing it', () => {
    useTaskStore.getState().setTask(makeTask())

    const { result } = renderHook(() =>
      useTaskQueue({ projectId: 'p1', type: 'annotate', queueMode: false, onClaimed: vi.fn() }),
    )
    act(() => result.current.finish())

    expect(result.current.task).toBeNull()
    expect(api.releaseTask).not.toHaveBeenCalled()
  })
})
