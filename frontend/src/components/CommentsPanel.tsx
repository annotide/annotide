import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useComments, useCreateComment, useResolveComment } from '@/api/queries'
import type { Comment } from '@/api/types'
import { ApiError } from '@/api/client'
import { currentLocale } from '@/i18n'
import { Button } from './Button'
import { Spinner } from './Spinner'
import { ErrorState } from './ErrorState'

export interface CommentsPanelProps {
  itemId: string
  /** Scopes new top-level comments to a specific annotation, when given. */
  annotationId?: string
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? (error.detail ?? error.title) : fallback
}

/** First 8 characters of a UUID — there is no user lookup on the frontend yet. */
function shortAuthor(authorId: string): string {
  return authorId.slice(0, 8)
}

function formatRelativeTime(iso: string): string {
  const deltaMs = Date.parse(iso) - Date.now()
  const deltaSeconds = Math.round(deltaMs / 1000)
  const divisions: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ['year', 60 * 60 * 24 * 365],
    ['month', 60 * 60 * 24 * 30],
    ['day', 60 * 60 * 24],
    ['hour', 60 * 60],
    ['minute', 60],
  ]
  const locale = currentLocale()
  for (const [unit, secondsInUnit] of divisions) {
    if (Math.abs(deltaSeconds) >= secondsInUnit) {
      const value = Math.round(deltaSeconds / secondsInUnit)
      return new Intl.RelativeTimeFormat(locale, { numeric: 'auto' }).format(value, unit)
    }
  }
  return new Intl.RelativeTimeFormat(locale, { numeric: 'auto' }).format(deltaSeconds, 'second')
}

interface CommentBodyProps {
  comment: Comment
  onReply?: (id: string) => void
  onToggleResolve: (comment: Comment) => void
  resolvePending: boolean
}

/** The author/timestamp/body/actions block shared by top-level comments and replies. */
function CommentBody({ comment, onReply, onToggleResolve, resolvePending }: CommentBodyProps): JSX.Element {
  const { t } = useTranslation('annotator')
  const resolved = Boolean(comment.resolved_at)
  return (
    <>
      <div className="flex items-center justify-between gap-2 text-xs text-muted">
        <span className="font-medium text-ink">{shortAuthor(comment.author_id)}</span>
        <span>{formatRelativeTime(comment.created_at)}</span>
      </div>
      <p className="whitespace-pre-wrap text-sm text-ink">{comment.body}</p>
      <div className="mt-1 flex items-center gap-2 text-xs">
        {resolved && (
          <span className="rounded bg-line/40 px-1.5 py-0.5 text-muted">{t('comments.resolved')}</span>
        )}
        <Button
          variant="ghost"
          size="sm"
          className="px-1.5 py-0.5"
          onClick={() => onToggleResolve(comment)}
          disabled={resolvePending}
        >
          {resolved ? t('comments.reopen') : t('comments.resolve')}
        </Button>
        {onReply && (
          <Button variant="ghost" size="sm" className="px-1.5 py-0.5" onClick={() => onReply(comment.id)}>
            {t('comments.reply')}
          </Button>
        )}
      </div>
    </>
  )
}

interface CommentRowProps {
  comment: Comment
  replies: Comment[]
  onReply: (id: string) => void
  onToggleResolve: (comment: Comment) => void
  resolvePending: boolean
}

function CommentRow({ comment, replies, onReply, onToggleResolve, resolvePending }: CommentRowProps): JSX.Element {
  return (
    <li className={comment.resolved_at ? 'opacity-60' : undefined}>
      <CommentBody
        comment={comment}
        onReply={onReply}
        onToggleResolve={onToggleResolve}
        resolvePending={resolvePending}
      />
      {replies.length > 0 && (
        <ul className="ml-3 mt-2 space-y-2 border-l border-line pl-3">
          {replies.map((reply) => (
            <li key={reply.id} className={reply.resolved_at ? 'opacity-60' : undefined}>
              <CommentBody comment={reply} onToggleResolve={onToggleResolve} resolvePending={resolvePending} />
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

/** The comment thread on an item (WF-5): top-level comments oldest first, with
 * one level of indented replies, a resolve/reopen toggle, and a composer that
 * can reply to a specific comment or post a new top-level one. */
export function CommentsPanel({ itemId, annotationId }: CommentsPanelProps): JSX.Element {
  const { t } = useTranslation(['annotator', 'common'])
  const commentsQuery = useComments(itemId)
  const createComment = useCreateComment(itemId)
  const resolveComment = useResolveComment(itemId)

  const [body, setBody] = useState('')
  const [replyTo, setReplyTo] = useState<string | null>(null)

  const comments = commentsQuery.data ?? []
  const topLevel = comments.filter((comment) => !comment.parent_id)
  const repliesByParent = new Map<string, Comment[]>()
  for (const comment of comments) {
    if (comment.parent_id) {
      const list = repliesByParent.get(comment.parent_id) ?? []
      list.push(comment)
      repliesByParent.set(comment.parent_id, list)
    }
  }

  const handlePost = (): void => {
    const trimmed = body.trim()
    if (!trimmed) return
    createComment.mutate(
      {
        body: trimmed,
        annotation_id: annotationId,
        parent_id: replyTo ?? undefined,
      },
      {
        onSuccess: () => {
          setBody('')
          setReplyTo(null)
        },
      },
    )
  }

  if (commentsQuery.isLoading) {
    return <Spinner label={t('comments.loading')} />
  }

  if (commentsQuery.isError) {
    return (
      <ErrorState
        title={t('comments.loadError')}
        message={errorMessage(commentsQuery.error, t('shared.unknownError'))}
        onRetry={() => void commentsQuery.refetch()}
      />
    )
  }

  return (
    <div className="flex flex-col gap-3">
      {topLevel.length === 0 ? (
        <p className="text-sm text-muted">{t('comments.noComments')}</p>
      ) : (
        <ul className="space-y-3">
          {topLevel.map((comment) => (
            <CommentRow
              key={comment.id}
              comment={comment}
              replies={repliesByParent.get(comment.id) ?? []}
              onReply={setReplyTo}
              onToggleResolve={(target) =>
                resolveComment.mutate({ id: target.id, resolved: !target.resolved_at })
              }
              resolvePending={resolveComment.isPending}
            />
          ))}
        </ul>
      )}

      <div className="border-t border-line pt-3">
        {replyTo && (
          <div className="mb-1 flex items-center justify-between text-xs text-muted">
            <span>{t('comments.replyingTo', { author: shortAuthor(replyTo) })}</span>
            <button type="button" className="underline" onClick={() => setReplyTo(null)}>
              {t('common:cancel')}
            </button>
          </div>
        )}
        <label htmlFor="comment-body" className="mb-1 block text-sm font-medium text-ink">
          {t('comments.newComment')}
        </label>
        <textarea
          id="comment-body"
          value={body}
          onChange={(event) => setBody(event.target.value)}
          placeholder={t('comments.placeholder')}
          rows={3}
          className="mb-2 w-full rounded-md border border-line bg-surface p-2 text-sm text-ink
            focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
            focus-visible:outline-accent"
        />
        {createComment.isError && (
          <p className="mb-2 text-xs text-danger">
            {errorMessage(createComment.error, t('comments.postError'))}
          </p>
        )}
        <Button
          variant="primary"
          size="sm"
          onClick={handlePost}
          disabled={createComment.isPending || body.trim().length === 0}
        >
          {createComment.isPending ? t('comments.posting') : t('comments.post')}
        </Button>
      </div>
    </div>
  )
}
