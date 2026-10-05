import type { ReactNode } from 'react'

export interface EmptyStateProps {
  title: string
  message?: string
  action?: ReactNode
}

/** A generic empty-state panel, used when a list query returns zero items. */
export function EmptyState({ title, message, action }: EmptyStateProps): JSX.Element {
  return (
    <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed border-line bg-surface p-10 text-center">
      <p className="text-base font-semibold text-ink">{title}</p>
      {message && <p className="max-w-prose text-sm text-muted">{message}</p>}
      {action}
    </div>
  )
}
