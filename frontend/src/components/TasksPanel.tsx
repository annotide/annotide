import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMembers, useTasks, useUpdateTask } from '@/api/queries'
import { ApiError } from '@/api/client'
import type { Item, Task } from '@/api/types'
import { currentLocale } from '@/i18n'
import { useAuthStore } from '@/lib/store'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { ErrorState } from '@/components/ErrorState'
import { EmptyState } from '@/components/EmptyState'

const PAGE_LIMIT = 50

export interface TasksPanelProps {
  projectId: string
  /** Items already loaded by the page, used to show paths instead of ids. */
  items?: Item[]
}

/** Queue order (WF-6): priority DESC, deadline ASC nulls last, created ASC —
 * the same order `POST /tasks/next` uses, so the table shows what is handed
 * out next. Exported for tests. */
export function queueOrder(a: Task, b: Task): number {
  if (a.priority !== b.priority) return b.priority - a.priority
  if (a.deadline !== b.deadline) {
    if (a.deadline === null) return 1
    if (b.deadline === null) return -1
    return a.deadline.localeCompare(b.deadline)
  }
  return a.created_at.localeCompare(b.created_at)
}

/** ISO → value for `<input type="datetime-local">` in the viewer's zone. */
function toLocalInput(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function isOverdue(task: Task, now: number): boolean {
  return task.deadline !== null && new Date(task.deadline).getTime() < now
}

function formatDeadline(iso: string): string {
  return new Date(iso).toLocaleString(currentLocale(), {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/** Open and in-progress tasks in queue order, with priority / deadline /
 * assignee editing for owners and reviewers (WF-6). Others see it read-only;
 * the server enforces the role either way. */
export function TasksPanel({ projectId, items = [] }: TasksPanelProps): JSX.Element {
  const { t } = useTranslation('projects')
  const openQuery = useTasks(projectId, { status: 'open', limit: PAGE_LIMIT })
  const inProgressQuery = useTasks(projectId, {
    status: 'in_progress',
    limit: PAGE_LIMIT,
  })
  const membersQuery = useMembers(projectId)
  const currentUser = useAuthStore((s) => s.user)
  const updateTask = useUpdateTask(projectId)
  const [error, setError] = useState<string | null>(null)
  const [priorityDrafts, setPriorityDrafts] = useState<Record<string, string>>({})

  const members = membersQuery.data ?? []
  const me = members.find((m) => m.user_id === currentUser?.id)
  const canEdit =
    Boolean(currentUser?.is_superuser) || me?.role === 'owner' || me?.role === 'reviewer'

  const memberName = new Map(members.map((m) => [m.user_id, m.display_name || m.email]))
  const itemPath = new Map(items.map((i) => [i.id, i.path]))

  const tasks = [...(openQuery.data?.items ?? []), ...(inProgressQuery.data?.items ?? [])].sort(
    queueOrder,
  )
  const truncated =
    Boolean(openQuery.data?.next_cursor) || Boolean(inProgressQuery.data?.next_cursor)
  const now = Date.now()

  function patch(task: Task, body: Parameters<typeof updateTask.mutate>[0]['patch']): void {
    setError(null)
    updateTask.mutate(
      { id: task.id, patch: body },
      {
        onError: (err) => {
          setError(err instanceof ApiError ? (err.detail ?? err.message) : t('tasks.updateFailed'))
        },
      },
    )
  }

  function commitPriority(task: Task): void {
    const draft = priorityDrafts[task.id]
    if (draft === undefined) return
    const value = Number.parseInt(draft, 10)
    setPriorityDrafts((prev) => {
      const next = { ...prev }
      delete next[task.id]
      return next
    })
    if (Number.isNaN(value) || value === task.priority) return
    patch(task, { priority: value })
  }

  const isLoading = openQuery.isLoading || inProgressQuery.isLoading
  const isError = openQuery.isError || inProgressQuery.isError

  return (
    <section aria-labelledby="tasks-heading" className="mb-8">
      <h2 id="tasks-heading" className="mb-1 text-lg font-semibold text-ink">
        {t('tasks.heading')}
      </h2>
      <p className="mb-3 text-sm text-muted">
        {t('tasks.description')}
        {truncated && t('tasks.truncated', { limit: PAGE_LIMIT })}
      </p>

      {isLoading && (
        <div className="flex justify-center py-6">
          <Spinner label={t('tasks.loading')} />
        </div>
      )}

      {isError && (
        <ErrorState
          title={t('tasks.loadError')}
          message={t('tasks.loadErrorMessage')}
          onRetry={() => {
            void openQuery.refetch()
            void inProgressQuery.refetch()
          }}
        />
      )}

      {!isLoading && !isError && tasks.length === 0 && (
        <EmptyState title={t('tasks.empty.title')} message={t('tasks.empty.message')} />
      )}

      {!isLoading && !isError && tasks.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-line text-muted">
                <th className="py-1.5 pr-2 font-medium">{t('tasks.columns.item')}</th>
                <th className="py-1.5 pr-2 font-medium">{t('tasks.columns.type')}</th>
                <th className="py-1.5 pr-2 font-medium">{t('tasks.columns.status')}</th>
                <th className="py-1.5 pr-2 font-medium">{t('tasks.columns.assignee')}</th>
                <th className="py-1.5 pr-2 font-medium">{t('tasks.columns.priority')}</th>
                <th className="py-1.5 pr-2 font-medium">{t('tasks.columns.deadline')}</th>
              </tr>
            </thead>
            <tbody>
              {tasks.map((task) => {
                const overdue = isOverdue(task, now)
                const label = itemPath.get(task.item_id) ?? task.item_id.slice(0, 8)
                return (
                  <tr
                    key={task.id}
                    className="border-b border-line/50"
                    data-testid={`task-row-${task.id}`}
                  >
                    <td className="py-1.5 pr-2 font-mono text-xs text-ink" title={task.item_id}>
                      {label}
                    </td>
                    <td className="py-1.5 pr-2 text-ink">{task.type}</td>
                    <td className="py-1.5 pr-2 text-ink">
                      {task.status === 'in_progress' ? t('tasks.inProgress') : task.status}
                    </td>
                    <td className="py-1.5 pr-2">
                      {canEdit ? (
                        <select
                          aria-label={t('tasks.assigneeLabel', { item: label })}
                          value={task.assignee_id ?? ''}
                          disabled={task.status === 'in_progress'}
                          onChange={(e) =>
                            patch(task, {
                              assignee_id: e.target.value === '' ? null : e.target.value,
                            })
                          }
                          className="rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink disabled:opacity-60"
                        >
                          <option value="">{t('tasks.anyone')}</option>
                          {members.map((m) => (
                            <option key={m.user_id} value={m.user_id}>
                              {m.display_name || m.email}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <span className="text-ink">
                          {task.assignee_id
                            ? (memberName.get(task.assignee_id) ?? task.assignee_id.slice(0, 8))
                            : t('tasks.anyone')}
                        </span>
                      )}
                    </td>
                    <td className="py-1.5 pr-2">
                      {canEdit ? (
                        <input
                          type="number"
                          aria-label={t('tasks.priorityLabel', { item: label })}
                          value={priorityDrafts[task.id] ?? String(task.priority)}
                          onChange={(e) =>
                            setPriorityDrafts((prev) => ({
                              ...prev,
                              [task.id]: e.target.value,
                            }))
                          }
                          onBlur={() => commitPriority(task)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') commitPriority(task)
                          }}
                          className="w-20 rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink"
                        />
                      ) : (
                        <span className="text-ink">{task.priority}</span>
                      )}
                    </td>
                    <td className="py-1.5 pr-2">
                      <div className="flex items-center gap-2">
                        {canEdit ? (
                          <input
                            type="datetime-local"
                            aria-label={t('tasks.deadlineLabel', { item: label })}
                            value={toLocalInput(task.deadline)}
                            onChange={(e) => {
                              if (e.target.value === '') return
                              patch(task, {
                                deadline: new Date(e.target.value).toISOString(),
                              })
                            }}
                            className={`rounded-md border bg-surface px-2 py-1 text-sm text-ink ${
                              overdue ? 'border-red-400' : 'border-line'
                            }`}
                          />
                        ) : (
                          <span className={overdue ? 'text-danger' : 'text-ink'}>
                            {task.deadline ? formatDeadline(task.deadline) : '—'}
                          </span>
                        )}
                        {overdue && (
                          <span className="rounded bg-red-500/10 px-1.5 py-0.5 text-xs text-danger">
                            {t('tasks.overdue')}
                          </span>
                        )}
                        {canEdit && task.deadline && (
                          <Button
                            variant="ghost"
                            size="sm"
                            aria-label={t('tasks.clearDeadlineLabel', { item: label })}
                            onClick={() => patch(task, { deadline: null })}
                          >
                            {t('tasks.clear')}
                          </Button>
                        )}
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}

      {error && (
        <p className="mt-3 text-sm text-danger" role="alert">
          {error}
        </p>
      )}
    </section>
  )
}
