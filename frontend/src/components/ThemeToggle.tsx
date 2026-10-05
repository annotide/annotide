import { useTranslation } from 'react-i18next'
import { useUiStore } from '@/lib/store'

/** Toggles the `dark` class on <html> to switch between the light/dark themes (UX-7). */
export function ThemeToggle(): JSX.Element {
  const { t } = useTranslation('shell')
  const theme = useUiStore((state) => state.theme)
  const toggleTheme = useUiStore((state) => state.toggleTheme)
  const isDark = theme === 'dark'
  const label = isDark ? t('theme.toLight') : t('theme.toDark')

  return (
    <button
      type="button"
      onClick={toggleTheme}
      aria-pressed={isDark}
      aria-label={label}
      title={label}
      className="inline-flex h-9 w-9 items-center justify-center rounded-md border border-line
        text-ink transition-colors hover:bg-line/30 focus-visible:outline
        focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
    >
      <span aria-hidden="true">{isDark ? '🌙' : '☀️'}</span>
    </button>
  )
}
