import { useEffect } from 'react'

import { useUiStore } from '@/lib/store'

/**
 * Applies the selected theme to the document (UX-7).
 *
 * The store holds the preference; Tailwind is configured with
 * `darkMode: 'class'`, so something has to put that class on `<html>`. Without
 * this hook the toggle flips state and the icon changes while the page stays
 * exactly as it was — which is what it did before this existed.
 *
 * `color-scheme` is set alongside the class so the browser's own widgets —
 * scrollbars, form controls, the caret — follow the theme too.
 */
export function useThemeClass(): void {
  const theme = useUiStore((state) => state.theme)

  useEffect(() => {
    const root = document.documentElement
    root.classList.toggle('dark', theme === 'dark')
    root.style.colorScheme = theme
  }, [theme])
}
