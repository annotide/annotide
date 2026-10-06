import { useEffect, useId, useRef } from 'react'
import type { KeyboardEvent } from 'react'
import { useTranslation } from 'react-i18next'

export interface ShortcutGroup {
  title: string
  items: ReadonlyArray<{ keys: string; label: string }>
}

interface ShortcutsDialogProps {
  open: boolean
  onClose: () => void
  groups: ReadonlyArray<ShortcutGroup>
}

/**
 * Modal cheat sheet of every keyboard shortcut on the page (UX-1), opened
 * with `?`. While it is open no key reaches the canvas or the page.
 */
export function ShortcutsDialog({ open, onClose, groups }: ShortcutsDialogProps): JSX.Element | null {
  const { t } = useTranslation('annotator')
  const titleId = useId()
  const closeRef = useRef<HTMLButtonElement>(null)
  const returnTo = useRef<Element | null>(null)

  useEffect(() => {
    if (!open) return undefined
    returnTo.current = document.activeElement
    closeRef.current?.focus()
    return () => {
      if (returnTo.current instanceof HTMLElement) returnTo.current.focus()
    }
  }, [open])

  if (!open) return null

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    // The canvas and the page listen on window; keep every key in here.
    event.nativeEvent.stopPropagation()
    if (event.key === 'Escape') {
      event.preventDefault()
      onClose()
    } else if (event.key === 'Tab') {
      // The close button is the only stop: keep focus inside the dialog.
      event.preventDefault()
      closeRef.current?.focus()
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={onKeyDown}
        className="max-h-[85vh] w-full max-w-2xl overflow-y-auto rounded-lg border border-line
          bg-surface p-5 text-ink shadow-xl"
      >
        <div className="mb-4 flex items-center justify-between gap-4">
          <h2 id={titleId} className="text-lg font-semibold">
            {t('shortcuts.title')}
          </h2>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            className="rounded-md border border-line px-2 py-1 text-sm hover:bg-line/30
              focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
              focus-visible:outline-accent"
          >
            {t('shortcuts.close')}
          </button>
        </div>
        <div className="grid gap-6 sm:grid-cols-2">
          {groups
            .filter((group) => group.items.length > 0)
            .map((group) => (
              <section key={group.title}>
                <h3 className="mb-2 text-sm font-semibold">{group.title}</h3>
                <dl className="space-y-1 text-sm">
                  {group.items.map((item) => (
                    <div key={`${item.keys} ${item.label}`} className="flex gap-3">
                      <dt className="w-28 shrink-0">
                        <kbd className="rounded border border-line px-1.5 py-0.5 text-xs">
                          {item.keys}
                        </kbd>
                      </dt>
                      <dd className="text-muted">{item.label}</dd>
                    </div>
                  ))}
                </dl>
              </section>
            ))}
        </div>
      </div>
    </div>
  )
}
