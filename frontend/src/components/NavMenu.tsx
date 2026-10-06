import { useEffect, useId, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Link, useLocation } from 'react-router-dom'

export interface NavMenuLink {
  to: string
  label: string
}

interface NavMenuProps {
  /** Text of the toggle button; also its accessible name. */
  label: string
  links: ReadonlyArray<NavMenuLink>
  /** Extra entries after the links, e.g. a sign-out button. */
  footer?: ReactNode
  /** Right-align the panel, for a menu at the end of the bar. */
  alignEnd?: boolean
  className?: string
  /** `data-testid` of the toggle, where its label is not stable (a name). */
  testId?: string
}

/**
 * Disclosure navigation (WAI-ARIA APG "disclosure navigation menu"): a
 * button that shows a list of links. Escape, a click outside or following
 * a link closes it.
 */
export function NavMenu({
  label,
  links,
  footer,
  alignEnd = false,
  className = '',
  testId,
}: NavMenuProps): JSX.Element {
  const [open, setOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const panelId = useId()
  const { pathname } = useLocation()
  const current = links.some((link) => pathname.startsWith(link.to))

  useEffect(() => {
    setOpen(false)
  }, [pathname])

  useEffect(() => {
    if (!open) return undefined
    function onKeyDown(event: KeyboardEvent): void {
      if (event.key !== 'Escape') return
      setOpen(false)
      buttonRef.current?.focus()
    }
    function onPointerDown(event: MouseEvent): void {
      if (!containerRef.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('keydown', onKeyDown)
    document.addEventListener('mousedown', onPointerDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.removeEventListener('mousedown', onPointerDown)
    }
  }, [open])

  return (
    <div ref={containerRef} className="relative">
      <button
        ref={buttonRef}
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        data-testid={testId}
        onClick={() => setOpen((value) => !value)}
        className={`inline-flex items-center gap-1 transition-colors hover:text-ink
          focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2
          focus-visible:outline-accent ${current ? 'text-ink' : 'text-muted'} ${className}`}
      >
        <span className="truncate">{label}</span>
        <span aria-hidden="true" className="text-xs">
          ▾
        </span>
      </button>
      <div
        id={panelId}
        hidden={!open}
        className={`absolute top-full z-40 mt-2 min-w-44 rounded-md border border-line
          bg-surface py-1 shadow-lg ${alignEnd ? 'right-0' : 'left-0'}`}
      >
        <ul>
          {links.map((link) => (
            <li key={link.to}>
              <Link
                to={link.to}
                aria-current={pathname.startsWith(link.to) ? 'page' : undefined}
                className="block px-3 py-1.5 text-sm text-ink hover:bg-line/30
                  focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2
                  focus-visible:outline-accent aria-[current=page]:font-semibold"
              >
                {link.label}
              </Link>
            </li>
          ))}
        </ul>
        {footer && <div className="mt-1 border-t border-line pt-1">{footer}</div>}
      </div>
    </div>
  )
}
