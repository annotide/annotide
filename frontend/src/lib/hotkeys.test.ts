import { renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useHotkeys } from './hotkeys'

function dispatchKeyDown(init: KeyboardEventInit, target: EventTarget = window): void {
  const event = new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init })
  target.dispatchEvent(event)
}

describe('useHotkeys', () => {
  afterEach(() => {
    document.body.innerHTML = ''
  })

  it('invokes the handler for a plain key press', () => {
    const handler = vi.fn()
    renderHook(() => useHotkeys({ n: handler }))

    dispatchKeyDown({ key: 'n' })

    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('invokes the handler for a mod+key combo', () => {
    const handler = vi.fn()
    renderHook(() => useHotkeys({ 'mod+z': handler }))

    dispatchKeyDown({ key: 'z', ctrlKey: true })

    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('invokes the handler for a shift combo', () => {
    const handler = vi.fn()
    renderHook(() => useHotkeys({ 'shift+?': handler }))

    dispatchKeyDown({ key: '?', shiftKey: true })

    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('does not fire when the target is an input element', () => {
    const handler = vi.fn()
    renderHook(() => useHotkeys({ n: handler }))

    const input = document.createElement('input')
    document.body.appendChild(input)
    input.focus()

    dispatchKeyDown({ key: 'n' }, input)

    expect(handler).not.toHaveBeenCalled()
  })

  it('does not fire when the target is contenteditable', () => {
    const handler = vi.fn()
    renderHook(() => useHotkeys({ n: handler }))

    const div = document.createElement('div')
    div.setAttribute('contenteditable', 'true')
    document.body.appendChild(div)

    dispatchKeyDown({ key: 'n' }, div)

    expect(handler).not.toHaveBeenCalled()
  })

  it('does not fire when disabled', () => {
    const handler = vi.fn()
    renderHook(() => useHotkeys({ n: handler }, false))

    dispatchKeyDown({ key: 'n' })

    expect(handler).not.toHaveBeenCalled()
  })

  describe('fallback', () => {
    afterEach(() => {
      vi.useRealTimers()
    })

    it('fires after the dispatch when no listener consumed the key', () => {
      vi.useFakeTimers()
      const handler = vi.fn()
      renderHook(() => useHotkeys({ p: handler }, true, { fallback: true }))

      dispatchKeyDown({ key: 'p' })
      expect(handler).not.toHaveBeenCalled()
      vi.runAllTimers()

      expect(handler).toHaveBeenCalledTimes(1)
    })

    it('skips a key consumed by a listener registered later', () => {
      vi.useFakeTimers()
      const handler = vi.fn()
      renderHook(() => useHotkeys({ p: handler }, true, { fallback: true }))
      const consume = (event: KeyboardEvent): void => event.preventDefault()
      window.addEventListener('keydown', consume)

      dispatchKeyDown({ key: 'p' })
      vi.runAllTimers()
      window.removeEventListener('keydown', consume)

      expect(handler).not.toHaveBeenCalled()
    })

    it('drops a pending key on unmount', () => {
      vi.useFakeTimers()
      const handler = vi.fn()
      const { unmount } = renderHook(() => useHotkeys({ p: handler }, true, { fallback: true }))

      dispatchKeyDown({ key: 'p' })
      unmount()
      vi.runAllTimers()

      expect(handler).not.toHaveBeenCalled()
    })
  })
})
