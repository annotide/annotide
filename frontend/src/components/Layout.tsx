import { useEffect, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, Outlet, useLocation } from 'react-router-dom'

import { api } from '@/api/client'
import { DocumentTitle } from './DocumentTitle'
import { isSsoToken } from '@/lib/sso'
import { useAuthStore } from '@/lib/store'
import { isMfaSetupToken } from '@/lib/token'
import { LanguageSelect } from './LanguageSelect'
import { LicenseBanner } from './LicenseBanner'
import { NavMenu } from './NavMenu'
import { NotificationsBell } from './NotificationsBell'
import { ThemeToggle } from './ThemeToggle'

const NAV_LINK_CLASS =
  'text-muted transition-colors hover:text-ink focus-visible:outline focus-visible:outline-2 ' +
  'focus-visible:outline-offset-2 focus-visible:outline-accent'

/** Application shell: top bar with branding, account controls and a routed outlet. */
export function Layout(): JSX.Element {
  const { t } = useTranslation('shell')
  const user = useAuthStore((state) => state.user)
  const logout = useAuthStore((state) => state.logout)
  const token = useAuthStore((state) => state.token)
  // AUTH-2 policy: a set-up-only token reaches no notifications or licence.
  const setupOnly = isMfaSetupToken(token)

  // An SSO session also ends at the provider, or the next sign-in would be
  // silent and "Sign out" would not mean much (RP-initiated logout).
  // A route change in a single-page app is silent to a screen reader; moving
  // focus to the new page's content announces it (UX-7, WCAG 2.4.3). Not on
  // the first render, where the browser's own focus is right.
  const mainRef = useRef<HTMLElement>(null)
  const { pathname } = useLocation()
  const inProjects = pathname === '/' || pathname.startsWith('/projects')
  const firstPath = useRef(pathname)
  useEffect(() => {
    if (pathname === firstPath.current) return
    firstPath.current = ''
    mainRef.current?.focus({ preventScroll: true })
  }, [pathname])

  const signOut = (): void => {
    const sso = isSsoToken(token)
    logout()
    if (sso) window.location.assign(api.oidcLogoutUrl())
  }

  return (
    <div className="flex min-h-screen flex-col bg-surface text-ink">
      <DocumentTitle />
      {/* First in the tab order, visible only when focused (WCAG 2.4.1). */}
      <a
        href="#main"
        onClick={(event) => {
          event.preventDefault()
          mainRef.current?.focus()
        }}
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50
          focus:rounded-md focus:bg-accent-fill focus:px-3 focus:py-2 focus:text-sm
          focus:text-white"
      >
        {t('skipToContent')}
      </a>
      {/* Wraps instead of scrolling sideways at 320 px (WCAG 1.4.10): the nav
          takes its own row below the brand and account controls on a phone. */}
      <header
        className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b
          border-line px-4 py-3 sm:px-6"
      >
        <Link
          to="/"
          className="text-sm font-semibold tracking-tight text-ink focus-visible:outline
            focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
        >
          Annotide
        </Link>
        {user && (
          <nav
            aria-label={t('nav.label')}
            className="order-last flex w-full flex-wrap items-center gap-x-4 gap-y-1 text-sm
              md:order-none md:w-auto"
          >
            <Link
              to="/"
              aria-current={pathname === '/' ? 'page' : undefined}
              className={`${NAV_LINK_CLASS} ${inProjects ? 'text-ink' : ''}`}
            >
              {t('nav.projects')}
            </Link>
            {/* Organisation-wide pages, superusers only, behind one menu
                instead of five links next to the everyday one. */}
            {user.is_superuser && (
              <NavMenu
                label={t('nav.admin')}
                links={[
                  { to: '/connectors', label: t('nav.connectors') },
                  { to: '/models', label: t('nav.models') },
                  { to: '/webhooks', label: t('nav.webhooks') },
                  { to: '/settings/users', label: t('nav.users') },
                  { to: '/settings/license', label: t('nav.licence') },
                ]}
              />
            )}
          </nav>
        )}
        <div className="flex flex-wrap items-center gap-3">
          <LanguageSelect />
          <ThemeToggle />
          {user && !setupOnly && <NotificationsBell />}
          {user && (
            // Personal settings and sign-out live under the user's own name.
            <NavMenu
              label={user.display_name}
              alignEnd
              testId="account-menu"
              className="max-w-40 text-sm"
              links={[
                { to: '/settings/api-keys', label: t('nav.apiKeys') },
                { to: '/settings/security', label: t('nav.security') },
              ]}
              footer={
                <button
                  type="button"
                  onClick={signOut}
                  className="block w-full px-3 py-1.5 text-left text-sm text-ink hover:bg-line/30
                    focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2
                    focus-visible:outline-accent"
                >
                  {t('signOut')}
                </button>
              }
            />
          )}
        </div>
      </header>
      {!setupOnly && <LicenseBanner />}
      <main id="main" ref={mainRef} tabIndex={-1} className="flex flex-1 flex-col outline-none">
        <Outlet />
      </main>
    </div>
  )
}
