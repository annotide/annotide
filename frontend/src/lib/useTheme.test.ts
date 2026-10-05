/**
 * Tests for theme application.
 *
 * The bug these guard against: the store held the preference and the toggle
 * button changed its icon, but nothing ever put the class on <html>, so
 * Tailwind's `darkMode: 'class'` never switched and the page never changed.
 * Every assertion here is about the DOM, not about store state — store state
 * was already correct while the feature was visibly broken.
 */

import { renderHook } from '@testing-library/react'
import { act } from 'react'
import { beforeEach, describe, expect, it } from 'vitest'

import { useUiStore } from '@/lib/store'
import { useThemeClass } from '@/lib/useTheme'

describe('useThemeClass', () => {
  beforeEach(() => {
    document.documentElement.className = ''
    document.documentElement.style.colorScheme = ''
    useUiStore.setState({ theme: 'dark' })
  })

  it('adds the dark class when the theme is dark', () => {
    useUiStore.setState({ theme: 'dark' })
    renderHook(() => useThemeClass())
    expect(document.documentElement.classList.contains('dark')).toBe(true)
  })

  it('removes the dark class when the theme is light', () => {
    useUiStore.setState({ theme: 'light' })
    renderHook(() => useThemeClass())
    expect(document.documentElement.classList.contains('dark')).toBe(false)
  })

  it('reacts to a theme change after mount', () => {
    useUiStore.setState({ theme: 'dark' })
    renderHook(() => useThemeClass())
    expect(document.documentElement.classList.contains('dark')).toBe(true)

    act(() => {
      useUiStore.getState().toggleTheme()
    })
    expect(document.documentElement.classList.contains('dark')).toBe(false)

    act(() => {
      useUiStore.getState().toggleTheme()
    })
    expect(document.documentElement.classList.contains('dark')).toBe(true)
  })

  it('sets color-scheme so browser widgets follow the theme', () => {
    useUiStore.setState({ theme: 'light' })
    renderHook(() => useThemeClass())
    expect(document.documentElement.style.colorScheme).toBe('light')
  })

  it('leaves other classes on the root element alone', () => {
    document.documentElement.classList.add('something-else')
    useUiStore.setState({ theme: 'dark' })
    renderHook(() => useThemeClass())

    expect(document.documentElement.classList.contains('something-else')).toBe(true)
    expect(document.documentElement.classList.contains('dark')).toBe(true)
  })
})
