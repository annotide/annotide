import { render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { LanguageSelect } from './LanguageSelect'
import { LANGUAGES, currentLocale, detectLanguage } from '@/i18n'

describe('LanguageSelect', () => {
  afterEach(() => {
    localStorage.clear()
  })

  it('renders nothing while English is the only language', () => {
    expect(LANGUAGES.map((language) => language.code)).toEqual(['en'])
    const { container } = render(<LanguageSelect />)
    expect(container).toBeEmptyDOMElement()
  })

  it('falls back to English for a browser language without a translation', () => {
    const languages = Object.getOwnPropertyDescriptor(Navigator.prototype, 'languages')
    Object.defineProperty(navigator, 'languages', { value: ['fi-FI', 'sv'], configurable: true })
    try {
      expect(detectLanguage()).toBe('en')
    } finally {
      delete (navigator as { languages?: unknown }).languages
      if (languages) Object.defineProperty(Navigator.prototype, 'languages', languages)
    }
  })

  it('ignores a stored language that no longer exists', () => {
    localStorage.setItem('annotation.lang', 'fi')
    expect(detectLanguage()).toBe('en')
    expect(currentLocale()).toBe('en-GB')
  })
})
