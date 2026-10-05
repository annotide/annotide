import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom'

import { api, ApiError, useAuthProviders } from '@/api'
import { Button } from '@/components/Button'
import { DocumentTitle } from '@/components/DocumentTitle'
import { LanguageSelect } from '@/components/LanguageSelect'
import { ThemeToggle } from '@/components/ThemeToggle'
import i18n from '@/i18n'
import { useAuthStore } from '@/lib/store'

interface LocationState {
  from?: string
}

/** The `?error=` codes the OIDC callback redirects back with (`services/oidc.py`). */
const SSO_ERROR_CODES = [
  'provider_denied',
  'provider_unreachable',
  'flow_expired',
  'state_mismatch',
  'token_exchange_failed',
  'invalid_id_token',
  'no_email',
  'email_unverified',
  'not_provisioned',
  'no_organization',
  'account_conflict',
  'inactive',
  'seat_limit',
  'login_required',
] as const

type SsoErrorCode = (typeof SSO_ERROR_CODES)[number]

function isSsoErrorCode(code: string): code is SsoErrorCode {
  return (SSO_ERROR_CODES as readonly string[]).includes(code)
}

/** Message for an SSO error code; anything unlisted gets a generic line. */
export function ssoErrorMessage(code: string): string {
  return i18n.t(isSsoErrorCode(code) ? `auth:sso.${code}` : 'auth:sso.generic')
}

/**
 * Sign-in page: local email-and-password (AUTH-2) plus a single sign-on
 * button when the installation has an identity provider (AUTH-1).
 *
 * The token goes into the auth store, which persists it and attaches it to
 * every later request. SSO leaves the page entirely: the backend sets a flow
 * cookie, sends the browser to the provider, and the provider sends it back
 * to `/auth/callback` (`AuthCallbackPage`) with the token in the fragment.
 * Small installations run without an identity provider at all, so the local
 * form stays.
 */
export function LoginPage(): JSX.Element {
  const { t } = useTranslation('auth')
  const navigate = useNavigate()
  const location = useLocation()
  const [searchParams] = useSearchParams()
  const login = useAuthStore((state) => state.login)
  const providers = useAuthProviders()

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  // AUTH-2: shown once the backend says the password was right but a code is needed.
  const [needsCode, setNeedsCode] = useState(false)
  const [otp, setOtp] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const redirectTo = (location.state as LocationState | null)?.from ?? '/'
  const ssoErrorCode = searchParams.get('error')
  const ssoProvider = providers.data?.oidc ?? null

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault()
    setError(null)
    setBusy(true)
    try {
      const token = await api.login({ email, password, ...(needsCode ? { otp } : {}) })
      // Fetch the profile with the fresh token so the store holds a real user
      // rather than one reconstructed from the form.
      const user = await api.me(token.access_token)
      login(token.access_token, user)
      navigate(redirectTo, { replace: true })
    } catch (caught) {
      if (caught instanceof ApiError && caught.type.endsWith('mfa-required')) {
        setNeedsCode(true)
        return
      }
      setError(
        caught instanceof ApiError
          ? caught.detail || caught.title
          : t('signIn.serverUnreachable'),
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen flex-col bg-surface text-ink">
      <DocumentTitle />
      <header className="flex items-center justify-between border-b border-line px-4 py-3">
        <span className="text-sm font-semibold">Annotide</span>
        <div className="flex items-center gap-3">
          <LanguageSelect />
          <ThemeToggle />
        </div>
      </header>

      <main className="flex flex-1 items-center justify-center p-4">
        <form
          onSubmit={handleSubmit}
          className="w-full max-w-sm rounded-lg border border-line bg-surface p-6 shadow-sm"
        >
          <h1 className="mb-1 text-lg font-semibold">{t('signIn.title')}</h1>
          <p className="mb-6 text-sm text-muted">{t('signIn.subtitle')}</p>

          {ssoErrorCode && !error && (
            <p role="alert" aria-live="assertive" className="mb-4 text-sm text-danger">
              {ssoErrorMessage(ssoErrorCode)}
            </p>
          )}

          {ssoProvider && (
            <>
              <a
                href={api.oidcLoginUrl(ssoProvider, redirectTo)}
                className="mb-4 inline-flex w-full items-center justify-center rounded-md border
                  border-line px-3 py-2 text-sm font-medium hover:bg-line/30
                  focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1
                  focus-visible:outline-accent"
              >
                {t('signIn.withProvider', { provider: ssoProvider.display_name })}
              </a>
              <div className="mb-4 flex items-center gap-3 text-xs uppercase text-muted">
                <span className="h-px flex-1 bg-line" aria-hidden="true" />
                {t('signIn.or')}
                <span className="h-px flex-1 bg-line" aria-hidden="true" />
              </div>
            </>
          )}

          <label htmlFor="email" className="mb-1 block text-sm font-medium">
            {t('signIn.email')}
          </label>
          <input
            id="email"
            name="email"
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            className="mb-4 w-full rounded-md border border-line bg-transparent px-3 py-2 text-sm
              focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1
              focus-visible:outline-accent"
          />

          <label htmlFor="password" className="mb-1 block text-sm font-medium">
            {t('signIn.password')}
          </label>
          <input
            id="password"
            name="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="mb-4 w-full rounded-md border border-line bg-transparent px-3 py-2 text-sm
              focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1
              focus-visible:outline-accent"
          />

          {needsCode && (
            <>
              <label htmlFor="otp" className="mb-1 block text-sm font-medium">
                {t('signIn.otp')}
              </label>
              <input
                id="otp"
                name="otp"
                inputMode="numeric"
                autoComplete="one-time-code"
                required
                autoFocus
                value={otp}
                onChange={(event) => setOtp(event.target.value)}
                className="mb-1 w-full rounded-md border border-line bg-transparent px-3 py-2
                  text-sm focus-visible:outline focus-visible:outline-2
                  focus-visible:outline-offset-1 focus-visible:outline-accent"
              />
              <p className="mb-4 text-xs text-muted">
                {t('signIn.otpHelp')}
              </p>
            </>
          )}

          {error && (
            <p role="alert" aria-live="assertive" className="mb-4 text-sm text-danger">
              {error}
            </p>
          )}

          <Button type="submit" disabled={busy} className="w-full">
            {busy ? t('signIn.submitting') : t('signIn.submit')}
          </Button>

          <p className="mt-6 text-xs text-muted">
            {t('signIn.firstAccount')}
            <code className="mt-1 block rounded bg-line/30 px-2 py-1 font-mono">
              docker compose exec backend python -m app.cli create-superuser --email you@example.com
            </code>
          </p>
        </form>
      </main>
    </div>
  )
}
