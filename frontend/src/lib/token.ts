/**
 * Reading claims of the platform's own access token. Never verifies it: the
 * server does that on every request; the browser only adapts what it shows.
 */

function claims(token: string | null | undefined): Record<string, unknown> | null {
  const payload = token?.split('.')[1]
  if (!payload) return null
  try {
    return JSON.parse(atob(payload.replace(/-/g, '+').replace(/_/g, '/'))) as Record<
      string,
      unknown
    >
  } catch {
    return null
  }
}

/** A token limited to setting up MFA (`APP_MFA_REQUIRED_FOR_ADMINS`, AUTH-2). */
export function isMfaSetupToken(token: string | null | undefined): boolean {
  return claims(token)?.mfa_setup === true
}
