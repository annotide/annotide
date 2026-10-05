import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import type { TFunction } from 'i18next'
import { useBulkItems, useMembers } from '@/api/queries'
import { ApiError } from '@/api/client'
import type { BulkAction, BulkRequest, BulkResult, TaskType } from '@/api/types'
import { Button } from '@/components/Button'

export interface BulkActionsBarProps {
  projectId: string
  selectedIds: string[]
  /** Path per item id, for the skipped-items list. */
  labels?: Map<string, string>
  onClear: () => void
  /** Called after a successful request so the page can refresh its grid. */
  onApplied?: (result: BulkResult) => void
}

function actionLabel(action: BulkAction, t: TFunction<'projects'>): string {
  return t(`bulk.actions.${action}`)
}

/** Split a comma / whitespace separated tag string into unique tags. */
export function parseTags(raw: string): string[] {
  return Array.from(new Set(raw.split(/[\s,]+/).filter(Boolean)))
}

/** Toolbar shown while items are selected in the grid (WF-8). Builds one
 * `POST /projects/{id}/items/bulk` request from the chosen action and its
 * fields, and reports how many items it applied to and why the rest were
 * skipped. Owner / reviewer only — the server enforces it. */
const BULK_ACTIONS: BulkAction[] = ['assign', 'return', 'approve', 'reject', 'tag']

export function BulkActionsBar({
  projectId,
  selectedIds,
  labels,
  onClear,
  onApplied,
}: BulkActionsBarProps): JSX.Element {
  const { t } = useTranslation('projects')
  const membersQuery = useMembers(projectId)
  const bulk = useBulkItems(projectId)

  const [action, setAction] = useState<BulkAction>('assign')
  const [taskType, setTaskType] = useState<TaskType>('annotate')
  const [assignee, setAssignee] = useState<string>('keep')
  const [priority, setPriority] = useState('')
  const [deadline, setDeadline] = useState('')
  const [comment, setComment] = useState('')
  const [addTags, setAddTags] = useState('')
  const [removeTags, setRemoveTags] = useState('')
  const [result, setResult] = useState<BulkResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [showSkipped, setShowSkipped] = useState(false)

  const members = membersQuery.data ?? []

  function buildRequest(): BulkRequest | null {
    const item_ids = selectedIds
    switch (action) {
      case 'assign': {
        const body: Extract<BulkRequest, { action: 'assign' }> = {
          action,
          item_ids,
          type: taskType,
        }
        if (assignee === 'none') body.assignee_id = null
        else if (assignee !== 'keep') body.assignee_id = assignee
        if (priority.trim() !== '') {
          const value = Number.parseInt(priority, 10)
          if (Number.isNaN(value)) return null
          body.priority = value
        }
        if (deadline !== '') body.deadline = new Date(deadline).toISOString()
        // With no field set this still opens missing tasks, so it is a valid request.
        return body
      }
      case 'return':
        return { action, item_ids }
      case 'approve':
        return comment.trim() ? { action, item_ids, comment: comment.trim() } : { action, item_ids }
      case 'reject':
        return comment.trim() ? { action, item_ids, comment: comment.trim() } : null
      case 'tag': {
        const add = parseTags(addTags)
        const remove = parseTags(removeTags)
        if (add.length === 0 && remove.length === 0) return null
        return { action, item_ids, add, remove }
      }
    }
  }

  function handleApply(): void {
    const body = buildRequest()
    if (!body) {
      setError(
        action === 'tag'
          ? t('bulk.tagError')
          : action === 'reject'
            ? t('bulk.rejectCommentError')
            : t('bulk.priorityError'),
      )
      return
    }
    setError(null)
    setResult(null)
    bulk.mutate(body, {
      onSuccess: (res) => {
        setResult(res)
        setShowSkipped(false)
        onApplied?.(res)
      },
      onError: (err) => {
        setError(err instanceof ApiError ? (err.detail ?? err.message) : t('bulk.genericError'))
      },
    })
  }

  const count = selectedIds.length
  const inputClass = 'rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink'

  return (
    <div
      role="region"
      aria-label={t('bulk.ariaLabel')}
      className="mb-4 rounded-lg border border-accent/40 bg-surface p-3"
    >
      <div className="flex flex-wrap items-end gap-3">
        <span className="text-sm font-medium text-ink" data-testid="bulk-count">
          {t('bulk.selectedCount', { count })}
        </span>

        <label className="flex flex-col gap-1 text-xs text-muted">
          {t('bulk.action')}
          <select
            aria-label={t('bulk.actionAria')}
            value={action}
            onChange={(e) => {
              setAction(e.target.value as BulkAction)
              setResult(null)
              setError(null)
            }}
            className={inputClass}
          >
            {BULK_ACTIONS.map((key) => (
              <option key={key} value={key}>
                {actionLabel(key, t)}
              </option>
            ))}
          </select>
        </label>

        {action === 'assign' && (
          <>
            <label className="flex flex-col gap-1 text-xs text-muted">
              {t('bulk.task')}
              <select
                aria-label={t('bulk.taskTypeLabel')}
                value={taskType}
                onChange={(e) => setTaskType(e.target.value as TaskType)}
                className={inputClass}
              >
                <option value="annotate">annotate</option>
                <option value="review">review</option>
              </select>
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted">
              {t('bulk.assignee')}
              <select
                aria-label={t('bulk.assignee')}
                value={assignee}
                onChange={(e) => setAssignee(e.target.value)}
                className={inputClass}
              >
                <option value="keep">{t('bulk.keepCurrent')}</option>
                <option value="none">{t('bulk.unassign')}</option>
                {members.map((m) => (
                  <option key={m.user_id} value={m.user_id}>
                    {m.display_name || m.email}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted">
              {t('bulk.priority')}
              <input
                type="number"
                aria-label={t('bulk.priority')}
                value={priority}
                placeholder="keep"
                onChange={(e) => setPriority(e.target.value)}
                className={`${inputClass} w-24`}
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted">
              {t('bulk.deadline')}
              <input
                type="datetime-local"
                aria-label={t('bulk.deadline')}
                value={deadline}
                onChange={(e) => setDeadline(e.target.value)}
                className={inputClass}
              />
            </label>
          </>
        )}

        {(action === 'approve' || action === 'reject') && (
          <label className="flex min-w-[16rem] flex-1 flex-col gap-1 text-xs text-muted">
            {action === 'reject' ? t('bulk.rejectComment') : t('bulk.comment')}
            <input
              type="text"
              aria-label={t('bulk.reviewCommentLabel')}
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              className={inputClass}
            />
          </label>
        )}

        {action === 'tag' && (
          <>
            <label className="flex flex-col gap-1 text-xs text-muted">
              {t('bulk.addTags')}
              <input
                type="text"
                aria-label={t('bulk.addTagsAria')}
                value={addTags}
                placeholder="night, blurry"
                onChange={(e) => setAddTags(e.target.value)}
                className={inputClass}
              />
            </label>
            <label className="flex flex-col gap-1 text-xs text-muted">
              {t('bulk.removeTags')}
              <input
                type="text"
                aria-label={t('bulk.removeTagsAria')}
                value={removeTags}
                onChange={(e) => setRemoveTags(e.target.value)}
                className={inputClass}
              />
            </label>
          </>
        )}

        <Button onClick={handleApply} disabled={count === 0 || bulk.isPending}>
          {bulk.isPending ? t('bulk.applying') : t('bulk.apply')}
        </Button>
        <Button variant="ghost" onClick={onClear} disabled={bulk.isPending}>
          {t('bulk.clearSelection')}
        </Button>
      </div>

      {error && (
        <p className="mt-2 text-sm text-danger" role="alert">
          {error}
        </p>
      )}

      {result && (
        <div className="mt-2 text-sm text-ink" role="status">
          {t('bulk.applied', { count: result.applied })}
          {result.skipped.length > 0 && (
            <>
              {t('bulk.skippedSuffix', { count: result.skipped.length })}{' '}
              <button
                type="button"
                className="text-accent underline-offset-2 hover:underline"
                onClick={() => setShowSkipped((v) => !v)}
              >
                {showSkipped ? t('bulk.hideReasons') : t('bulk.showReasons')}
              </button>
              {showSkipped && (
                <ul className="mt-1 list-disc pl-5 text-xs text-muted">
                  {result.skipped.map((s) => (
                    <li key={s.item_id}>
                      <span className="font-mono">
                        {labels?.get(s.item_id) ?? s.item_id.slice(0, 8)}
                      </span>
                      : {s.reason}
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
          {result.skipped.length === 0 && '.'}
        </div>
      )}
    </div>
  )
}
