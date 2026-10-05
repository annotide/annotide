import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'

import { useAuthStore } from '@/lib/store'
import { isMfaSetupToken } from '@/lib/token'

/**
 * Sends signed-out visitors to the sign-in page.
 *
 * Without this, an unauthenticated visit renders the app shell and every query
 * fails with a 401, which is what produced the bare "Could not load projects /
 * Provide an access token" screen with no way forward.
 *
 * The attempted path is passed along so sign-in can return the person to where
 * they were headed instead of dropping them on the dashboard.
 */
const MFA_SETUP_PATH = '/settings/security'

export function RequireAuth({ children }: { children: ReactNode }): JSX.Element {
  const token = useAuthStore((state) => state.token)
  const location = useLocation()

  if (!token) {
    return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  }
  // An administrator who must turn MFA on first (AUTH-2): the token reaches
  // nothing else, so every page but the security page would only fail.
  if (isMfaSetupToken(token) && location.pathname !== MFA_SETUP_PATH) {
    return <Navigate to={MFA_SETUP_PATH} replace />
  }

  return <>{children}</>
}
