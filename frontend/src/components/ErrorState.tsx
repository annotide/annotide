import { useTranslation } from 'react-i18next'
import { Button } from './Button'

export interface ErrorStateProps {
  title?: string
  message?: string
  onRetry?: () => void
}

/** A generic error panel with an optional retry action, announced via aria-live. */
export function ErrorState({
  title,
  message,
  onRetry,
}: ErrorStateProps): JSX.Element {
  const { t } = useTranslation()
  return (
    <div
      role="alert"
      aria-live="assertive"
      className="flex flex-col items-center gap-3 rounded-lg border border-line bg-surface p-8 text-center"
    >
      <p className="text-base font-semibold text-ink">{title ?? t('somethingWentWrong')}</p>
      {message && <p className="max-w-prose text-sm text-muted">{message}</p>}
      {onRetry && (
        <Button variant="secondary" size="sm" onClick={onRetry}>
          {t('tryAgain')}
        </Button>
      )}
    </div>
  )
}
