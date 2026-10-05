/**
 * Tests for the start-up session check.
 *
 * The bug these guard against: after `make reset` the database is empty but
 * the browser still holds a validly-signed token, so the app rendered as
 * signed in — for an account that no longer existed — and showed whatever it
 * had cached until some request happened to fail.
 */

import { renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { User } from '@/api/types'
import { useAuthStore } from '@/lib/store'
import { useSessionCheck } from '@/lib/useSession'

const me = vi.fn()

vi.mock('@/api/client', () => ({
  api: {
    me: (...args: unknown[]) => me(...args),
  },
}))

const user: User = {
  id: 'u1',
  organization_id: 'o1',
  email: 'me@example.com',
  display_name: 'Me',
  is_active: true,
  is_superuser: false,
  last_seen_at: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

describe('useSessionCheck', () => {
  beforeEach(() => {
    me.mockReset()
    useAuthStore.setState({ token: null, user: null })
  })

  it('does nothing when there is no stored session', () => {
    renderHook(() => useSessionCheck())
    expect(me).not.toHaveBeenCalled()
  })

  it('refreshes the cached user when the server still knows the account', async () => {
    useAuthStore.setState({ token: 'a-token', user: null })
    me.mockResolvedValue(user)

    renderHook(() => useSessionCheck())

    await waitFor(() => expect(useAuthStore.getState().user).toEqual(user))
    expect(useAuthStore.getState().token).toBe('a-token')
  })

  it('leaves a session cleared by the 401 handler cleared', async () => {
    useAuthStore.setState({ token: 'a-token', user: null })
    // The real client signs out on 401 before rejecting; mirror that.
    me.mockImplementation(async () => {
      useAuthStore.getState().logout()
      throw new Error('401')
    })

    renderHook(() => useSessionCheck())

    await waitFor(() => expect(me).toHaveBeenCalled())
    expect(useAuthStore.getState().token).toBeNull()
    expect(useAuthStore.getState().user).toBeNull()
  })

  it('keeps the session when the check fails for another reason', async () => {
    useAuthStore.setState({ token: 'a-token', user: null })
    me.mockRejectedValue(new TypeError('Failed to fetch'))

    renderHook(() => useSessionCheck())

    await waitFor(() => expect(me).toHaveBeenCalled())
    expect(useAuthStore.getState().token).toBe('a-token')
  })

  it('does not resurrect a session that changed while the check was in flight', async () => {
    useAuthStore.setState({ token: 'old-token', user: null })
    let resolve!: (value: User) => void
    me.mockReturnValue(
      new Promise<User>((r) => {
        resolve = r
      }),
    )

    renderHook(() => useSessionCheck())
    useAuthStore.getState().logout()
    resolve(user)

    await new Promise((r) => setTimeout(r, 0))
    expect(useAuthStore.getState().token).toBeNull()
  })
})
