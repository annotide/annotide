import { useTranslation } from 'react-i18next'
import { LANGUAGES, currentLanguage, setLanguage } from '@/i18n'
import type { Language } from '@/i18n'

/**
 * UI language picker (UX-8); the choice is remembered in this browser.
 * Renders nothing while there is only one language to choose from.
 */
export function LanguageSelect(): JSX.Element | null {
  const { t } = useTranslation('shell')
  // Re-renders on language change through useTranslation.
  const value = currentLanguage()
  if (LANGUAGES.length < 2) return null

  return (
    <select
      aria-label={t('language')}
      title={t('language')}
      value={value}
      onChange={(event) => void setLanguage(event.target.value as Language)}
      className="h-9 rounded-md border border-line bg-surface px-2 text-sm text-ink
        focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
        focus-visible:outline-accent"
    >
      {LANGUAGES.map((language) => (
        <option key={language.code} value={language.code} lang={language.code}>
          {language.label}
        </option>
      ))}
    </select>
  )
}
