import { useRef } from 'react'
import type { KeyboardEvent, ReactNode } from 'react'

export interface TabItem<T extends string> {
  id: T
  label: string
}

export interface TabsProps<T extends string> {
  /** Accessible name of the tab list. */
  label: string
  /** Prefix for the tab and panel element ids; unique on the page. */
  idPrefix: string
  tabs: ReadonlyArray<TabItem<T>>
  value: T
  onChange: (id: T) => void
}

/**
 * WAI-ARIA tabs: one tab stop, arrow keys / Home / End move and activate.
 * Pair each tab with a `TabPanel` of the same `idPrefix` and `id`.
 */
export function Tabs<T extends string>({
  label,
  idPrefix,
  tabs,
  value,
  onChange,
}: TabsProps<T>): JSX.Element {
  const refs = useRef(new Map<T, HTMLButtonElement>())

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    const index = tabs.findIndex((tab) => tab.id === value)
    const count = tabs.length
    const moves: Partial<Record<string, number>> = {
      ArrowRight: (index + 1) % count,
      ArrowLeft: (index - 1 + count) % count,
      Home: 0,
      End: count - 1,
    }
    const next = moves[event.key]
    if (next === undefined) return
    event.preventDefault()
    const id = tabs[next].id
    onChange(id)
    refs.current.get(id)?.focus()
  }

  return (
    <div
      role="tablist"
      aria-label={label}
      onKeyDown={onKeyDown}
      className="mb-6 flex flex-wrap gap-1 border-b border-line"
    >
      {tabs.map((tab) => {
        const selected = tab.id === value
        return (
          <button
            key={tab.id}
            ref={(element) => {
              if (element) refs.current.set(tab.id, element)
              else refs.current.delete(tab.id)
            }}
            type="button"
            role="tab"
            id={`${idPrefix}-tab-${tab.id}`}
            aria-selected={selected}
            aria-controls={`${idPrefix}-panel-${tab.id}`}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(tab.id)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium focus-visible:outline
              focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent
              ${selected ? 'border-accent text-ink' : 'border-transparent text-muted hover:text-ink'}`}
          >
            {tab.label}
          </button>
        )
      })}
    </div>
  )
}

export interface TabPanelProps {
  idPrefix: string
  id: string
  active: boolean
  children: ReactNode
}

/**
 * A tab's content. Inactive panels stay mounted but hidden, so an upload or
 * a half-filled form survives switching tabs.
 */
export function TabPanel({ idPrefix, id, active, children }: TabPanelProps): JSX.Element {
  return (
    <div
      role="tabpanel"
      id={`${idPrefix}-panel-${id}`}
      aria-labelledby={`${idPrefix}-tab-${id}`}
      hidden={!active}
    >
      {children}
    </div>
  )
}
