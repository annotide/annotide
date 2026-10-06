import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useLocation, useNavigate } from 'react-router-dom'

import { api } from '@/api'
import { useAuthStore } from '@/lib/store'

/** Reads the fragment the backend's OIDC callback redirects here with. */
export function parseCallbackFragment(hash: string): {
  token: string | null
  next: string
} {
  const params = new URLSearchParams(hash.startsWith('#') ? hash.slice(1) : hash)
  const next = params.get('next') ?? '/'
  return {
    token: params.get('access_token'),
    // The backend already refuses off-site paths; this is belt and braces.
    next: next.startsWith('/') && !next.startsWith('//') ? next : '/',
  }
}

/**
 * Landing page for single sign-on (AUTH-1).
 *
 * The backend finishes the OIDC flow and redirects the browser here with
 * `#access_token=…&expires_in=…&next=…`. The fragment never reaches a server,
 * which is why the token travels in it. This page moves the token into the
 * auth store, fetches the profile, scrubs the fragment from history and
 * continues to `next`.
 */
export function AuthCallbackPage(): JSX.Element {
  const { t } = useTranslation('auth')
  const navigate = useNavigate()
  const location = useLocation()
  const login = useAuthStore((state) => state.login)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    const { token, next } = parseCallbackFragment(location.hash)
    if (!token) {
      setFailed(true)
      return
    }
    let cancelled = false
    api
      .me(token)
      .then((user) => {
        if (cancelled) return
        login(token, user)
        // `replace` drops the URL carrying the token from the history stack.
        navigate(next, { replace: true })
      })
      .catch(() => {
        if (!cancelled) setFailed(true)
      })
    return () => {
      cancelled = true
    }
  }, [location.hash, login, navigate])

  return (
    <div className="flex min-h-screen items-center justify-center bg-surface p-4 text-ink">
      {failed ? (
        <div role="alert" className="max-w-sm text-center text-sm">
          <p className="mb-3">{t('callback.failed')}</p>
          <Link to="/login" className="text-accent underline">
            {t('callback.back')}
          </Link>
        </div>
      ) : (
        <p role="status" className="text-sm text-muted">
          {t('callback.pending')}
        </p>
      )}
    </div>
  )
}
