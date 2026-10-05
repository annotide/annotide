/**
 * i18next setup (UX-8), frontend only. English only for now; a language is
 * added by listing it in `LANGUAGES` and giving it a full set of locale files
 * in `resources.ts` (the parity test then checks it against English).
 *
 * The language is the viewer's choice, kept in localStorage; the first visit
 * follows the browser, falling back to English. Components
 * use `useTranslation('<namespace>')`; dates and numbers go through
 * `formatDateTime` / `formatNumber` so they follow the same language.
 */
import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'
import { NAMESPACES, resources } from './resources'

/** `locale` is the BCP 47 tag dates and numbers are formatted with. */
export const LANGUAGES = [{ code: 'en', label: 'English', locale: 'en-GB' }] as const

export type Language = (typeof LANGUAGES)[number]['code']

const STORAGE_KEY = 'annotation.lang'

function isLanguage(value: unknown): value is Language {
  return LANGUAGES.some((language) => language.code === value)
}

export function detectLanguage(): Language {
  try {
    const stored = localStorage.getItem(STORAGE_KEY)
    if (isLanguage(stored)) return stored
  } catch {
    // Storage unavailable: fall through to the browser language.
  }
  const preferred = typeof navigator === 'undefined' ? [] : (navigator.languages ?? [])
  for (const tag of preferred) {
    const code = tag.toLowerCase().split('-')[0]
    if (isLanguage(code)) return code
  }
  return 'en'
}

export function currentLanguage(): Language {
  return isLanguage(i18n.language) ? i18n.language : 'en'
}

/** The BCP 47 locale of the current language, for `Intl` formatters. */
export function currentLocale(): string {
  const code = currentLanguage()
  return LANGUAGES.find((language) => language.code === code)?.locale ?? 'en-GB'
}

export async function setLanguage(language: Language): Promise<void> {
  try {
    localStorage.setItem(STORAGE_KEY, language)
  } catch {
    // Ignore storage failures; the choice still applies to this tab.
  }
  await i18n.changeLanguage(language)
}

/** Locale-aware date and time, e.g. "28/09/2026, 08:45:00" in English. */
export function formatDateTime(value: string | number | Date): string {
  return new Date(value).toLocaleString(currentLocale())
}

/** Locale-aware date only. */
export function formatDateOnly(value: string | number | Date): string {
  return new Date(value).toLocaleDateString(currentLocale())
}

export function formatNumber(value: number, options?: Intl.NumberFormatOptions): string {
  return new Intl.NumberFormat(currentLocale(), options).format(value)
}

i18n.on('languageChanged', (language) => {
  if (typeof document !== 'undefined') document.documentElement.lang = language
})

void i18n.use(initReactI18next).init({
  resources,
  lng: detectLanguage(),
  fallbackLng: 'en',
  supportedLngs: LANGUAGES.map((language) => language.code),
  ns: NAMESPACES,
  defaultNS: 'common',
  interpolation: { escapeValue: false },
  returnNull: false,
})

export default i18n
