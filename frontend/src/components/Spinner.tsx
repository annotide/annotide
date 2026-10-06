import { useTranslation } from 'react-i18next'

export interface SpinnerProps {
  label?: string
  size?: number
}

/** A small accessible loading indicator; announces itself via aria-live. */
export function Spinner({ label, size = 20 }: SpinnerProps): JSX.Element {
  const { t } = useTranslation()
  return (
    <span role="status" aria-live="polite" className="inline-flex items-center gap-2 text-muted">
      <svg
        className="animate-spin text-accent"
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        aria-hidden="true"
      >
        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
        <path
          className="opacity-75"
          fill="currentColor"
          d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"
        />
      </svg>
      <span className="text-sm">{label ?? t('loading')}</span>
    </span>
  )
}
