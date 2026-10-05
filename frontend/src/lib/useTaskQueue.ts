/**
 * Claim-and-hold logic for the task queue (WF-2, WF-3), shared by the
 * annotate and review pages.
 *
 * A page is in *queue mode* when its route has no item: it asks the server
 * for the next open task of its type and navigates to that task's item. While
 * a task is held the hook heartbeats the lock (`POST /tasks/{id}/extend`) so a
 * long image does not outlive `APP_TASK_LOCK_TTL`, and releases the lock on
 * unmount if the page never finished or released it explicitly.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '@/api/client'
import type { Task, TaskType } from '@/api/types'
import { useTaskStore } from '@/lib/store'

/** How often a held lock is extended. Well inside the 30 min default TTL. */
export const HEARTBEAT_MS = 5 * 60 * 1000

export interface UseTaskQueueOptions {
  projectId: string | undefined
  type: TaskType
  /** True when the route carries no item, i.e. the page should claim. */
  queueMode: boolean
  /** Called with the claimed task so the page can navigate to its item. */
  onClaimed: (task: Task) => void
}

export interface TaskQueue {
  /** The task this browser holds, if any (also held across navigation). */
  task: Task | null
  /** True once a claim has answered "queue is empty". */
  empty: boolean
  claiming: boolean
  error: unknown
  /** Ask for the next task. Releases the current one first if still held. */
  claimNext: () => void
  /** Release the held lock without finishing the work (skip / leave). */
  release: () => Promise<void>
  /**
   * Forget the held task because the server has closed it (submit, review
   * verdict). Nothing is sent: the server-side transition already did.
   */
  finish: () => void
}

export function useTaskQueue({ projectId, type, queueMode, onClaimed }: UseTaskQueueOptions): TaskQueue {
  const task = useTaskStore((state) => state.task)
  const setTask = useTaskStore((state) => state.setTask)
  const clearTask = useTaskStore((state) => state.clearTask)

  const [empty, setEmpty] = useState(false)
  const [claiming, setClaiming] = useState(false)
  const [error, setError] = useState<unknown>(null)

  // Latest callback without re-arming effects on every render.
  const onClaimedRef = useRef(onClaimed)
  onClaimedRef.current = onClaimed
  // A claim in flight. StrictMode runs effects twice in development, and two
  // concurrent claims would lock two tasks and abandon one of them.
  const inFlight = useRef(false)

  const release = useCallback(async () => {
    const held = useTaskStore.getState().task
    if (!held) return
    clearTask()
    try {
      await api.releaseTask(held.id)
    } catch {
      // Best effort: the lock expires on its own if this fails.
    }
  }, [clearTask])

  const finish = useCallback(() => clearTask(), [clearTask])

  const claimNext = useCallback(() => {
    if (!projectId || inFlight.current) return
    inFlight.current = true
    setClaiming(true)
    setEmpty(false)
    setError(null)
    void (async () => {
      await release()
      try {
        const next = await api.nextTask({ project_id: projectId, type })
        if (next) {
          setTask(next)
          onClaimedRef.current(next)
        } else {
          setEmpty(true)
        }
      } catch (err) {
        setError(err)
      } finally {
        inFlight.current = false
        setClaiming(false)
      }
    })()
  }, [projectId, release, setTask, type])

  // Queue mode: claim once per entry. Entering queue mode with a task still
  // held (e.g. "next" pressed mid-item) releases it first, inside claimNext.
  useEffect(() => {
    if (!queueMode || !projectId) return
    claimNext()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queueMode, projectId, type])

  // Heartbeat while a task is held.
  useEffect(() => {
    if (!task) return
    const id = window.setInterval(() => {
      api.extendTask(task.id).catch(() => {
        // Ignore: a failed heartbeat means the lock may lapse, which the
        // server handles; nothing useful to do in the UI.
      })
    }, HEARTBEAT_MS)
    return () => window.clearInterval(id)
  }, [task])

  // Leaving the page with a lock still held: give it back.
  useEffect(
    () => () => {
      void release()
    },
    [release],
  )

  return { task, empty, claiming, error, claimNext, release, finish }
}
