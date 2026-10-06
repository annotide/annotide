import { useEffect } from 'react'

import { api } from '@/api/client'
import { useAuthStore } from '@/lib/store'

/**
 * Checks a restored session against the server once, on start-up.
 *
 * The token lives in localStorage and the API verifies it by signature alone,
 * so a token can outlive its account: reset the database (`make reset`) with
 * the same signing key and the browser still looks signed in — as a user and
 * organisation that no longer exist — until some request happens to 401.
 *
 * `/auth/me` is the one endpoint that does look the account up. Calling it
 * here means a dead session is cleared before the first page renders stale
 * data; the client's 401 handler does the actual sign-out. A fresh answer also
 * refreshes the cached user record. Network failures are ignored: an offline
 * app should keep its session, not lose it.
 */
export function useSessionCheck(): void {
  useEffect(() => {
    const token = useAuthStore.getState().token
    if (!token) return

    let cancelled = false
    api
      .me()
      .then((user) => {
        // Only refresh if this is still the same session; the person may have
        // signed out (or in again) while the request was in flight.
        if (!cancelled && useAuthStore.getState().token === token) {
          useAuthStore.getState().login(token, user)
        }
      })
      .catch(() => {
        // A 401 has already cleared the store; anything else is left alone.
      })

    return () => {
      cancelled = true
    }
  }, [])
}
