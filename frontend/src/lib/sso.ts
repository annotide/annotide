/**
 * Single sign-on session helpers (AUTH-1).
 *
 * A token issued through SSO carries `sso: true` (docs/CONTRACTS.md). For
 * such a session, signing out also ends the provider session, and an expired
 * token is renewed by one silent `prompt=none` round trip to the provider
 * instead of dropping the person on the login form.
 */

const SILENT_ATTEMPT_KEY = 'annotation.sso.silentAttemptAt'
/** A silent attempt within this window is not repeated: the provider said no. */
const SILENT_RETRY_MS = 60_000

/** Whether a platform token was issued by single sign-on. Never verifies it. */
export function isSsoToken(token: string | null | undefined): boolean {
  if (!token) return false
  const payload = token.split('.')[1]
  if (!payload) return false
  try {
    const json = atob(payload.replace(/-/g, '+').replace(/_/g, '/'))
    return (JSON.parse(json) as { sso?: unknown }).sso === true
  } catch {
    return false
  }
}

/**
 * Record a silent re-authentication attempt; `false` when one was made in the
 * last minute, so a provider without a session cannot cause a redirect loop.
 */
export function claimSilentAttempt(now: number = Date.now()): boolean {
  try {
    const last = Number(sessionStorage.getItem(SILENT_ATTEMPT_KEY) ?? 0)
    if (now - last < SILENT_RETRY_MS) return false
    sessionStorage.setItem(SILENT_ATTEMPT_KEY, String(now))
    return true
  } catch {
    // No session storage: never attempt, rather than risk a loop.
    return false
  }
}

/** Where the browser currently is, as the `next` path to come back to. */
export function currentPath(): string {
  return `${window.location.pathname}${window.location.search}`
}
