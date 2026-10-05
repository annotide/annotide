/**
 * useHotkeys — a small keyboard-shortcut hook (UX-1).
 *
 * Keys in `map` are either a plain key (`'n'`, `'?'`, `'Escape'`) or a
 * modifier combo joined with `+`, e.g. `'mod+z'` (mod = Cmd on Mac, Ctrl
 * elsewhere), `'shift+?'`, `'ctrl+shift+s'`. Matching is case-insensitive
 * on the base key.
 *
 * Keystrokes originating in <input>, <textarea>, <select>, or any
 * contenteditable element are ignored so hotkeys never fight with typing.
 *
 * Page-level shortcuts pass `{ fallback: true }`: they run after every other
 * keydown listener and skip a key that one of them consumed (called
 * `preventDefault`). A canvas or annotator that acts on a key marks it that
 * way, so pressing P for the point tool never also means "previous item".
 */
import { useEffect } from 'react'

type Handler = (event: KeyboardEvent) => void

export type HotkeyMap = Record<string, Handler>

interface ParsedCombo {
  key: string
  ctrl: boolean
  meta: boolean
  shift: boolean
  alt: boolean
}

function isMac(): boolean {
  if (typeof navigator === 'undefined') return false
  return /Mac|iPod|iPhone|iPad/.test(navigator.platform ?? navigator.userAgent ?? '')
}

function parseCombo(combo: string): ParsedCombo {
  const parts = combo
    .split('+')
    .map((part) => part.trim())
    .filter(Boolean)

  let ctrl = false
  let meta = false
  let shift = false
  let alt = false
  let key = ''

  for (const part of parts) {
    const lower = part.toLowerCase()
    if (lower === 'mod') {
      if (isMac()) meta = true
      else ctrl = true
    } else if (lower === 'ctrl' || lower === 'control') {
      ctrl = true
    } else if (lower === 'meta' || lower === 'cmd' || lower === 'command') {
      meta = true
    } else if (lower === 'shift') {
      shift = true
    } else if (lower === 'alt' || lower === 'option') {
      alt = true
    } else {
      key = part
    }
  }

  return { key: key.toLowerCase(), ctrl, meta, shift, alt }
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  const tag = target.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true
  if (target.isContentEditable) return true
  // Fall back to an attribute check: some test/DOM environments (notably
  // jsdom) don't implement the `isContentEditable` IDL reflection, and a
  // keystroke inside a nested element of an editable host should also count.
  if (target.closest('[contenteditable]:not([contenteditable="false"])')) return true
  return false
}

function eventMatchesCombo(event: KeyboardEvent, combo: ParsedCombo): boolean {
  const eventKey = event.key.toLowerCase()
  if (eventKey !== combo.key) return false
  if (event.ctrlKey !== combo.ctrl) return false
  if (event.metaKey !== combo.meta) return false
  if (event.altKey !== combo.alt) return false
  // Only enforce shift when the combo requires it or the key itself isn't
  // shift-dependent (so 'shift+?' still matches on layouts where '?' needs
  // shift, without requiring every consumer to think about that).
  if (combo.shift && !event.shiftKey) return false
  return true
}

export interface HotkeyOptions {
  /** Yield to any listener that consumed the key (see the module comment). */
  fallback?: boolean
}

/**
 * Registers global keydown handlers for the given hotkey map while the
 * component is mounted and `enabled` is true.
 */
export function useHotkeys(map: HotkeyMap, enabled = true, options: HotkeyOptions = {}): void {
  const { fallback = false } = options
  useEffect(() => {
    if (!enabled) return undefined

    const parsedEntries = Object.entries(map).map(
      ([combo, handler]) => [parseCombo(combo), handler] as const,
    )
    const pending = new Set<ReturnType<typeof setTimeout>>()

    function onKeyDown(event: KeyboardEvent): void {
      if (isEditableTarget(event.target)) return

      for (const [combo, handler] of parsedEntries) {
        if (eventMatchesCombo(event, combo)) {
          if (!fallback) {
            handler(event)
            return
          }
          // Listener order on window is registration order, which a page
          // cannot control; wait until the whole dispatch has finished.
          const timer = setTimeout(() => {
            pending.delete(timer)
            if (!event.defaultPrevented) handler(event)
          }, 0)
          pending.add(timer)
          return
        }
      }
    }

    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      for (const timer of pending) clearTimeout(timer)
    }
  }, [map, enabled, fallback])
}
