/**
 * Tests for the auth route guard.
 *
 * The bug these guard against: with no token, the app rendered its shell and
 * every query failed with 401, leaving "Could not load projects / Provide an
 * access token in the Authorization header" and no way to sign in.
 */

import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it } from 'vitest'

import { RequireAuth } from '@/components/RequireAuth'
import { useAuthStore } from '@/lib/store'

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/login" element={<p>Sign in page</p>} />
        <Route
          path="/settings/security"
          element={
            <RequireAuth>
              <p>Security page</p>
            </RequireAuth>
          }
        />
        <Route
          path="/secret"
          element={
            <RequireAuth>
              <p>Protected content</p>
            </RequireAuth>
          }
        />
      </Routes>
    </MemoryRouter>,
  )
}

describe('RequireAuth', () => {
  beforeEach(() => {
    useAuthStore.setState({ token: null, user: null })
  })

  it('redirects to the sign-in page when there is no token', () => {
    renderAt('/secret')
    expect(screen.getByText('Sign in page')).toBeInTheDocument()
    expect(screen.queryByText('Protected content')).not.toBeInTheDocument()
  })

  it('keeps an administrator with a set-up-only token on the security page (AUTH-2)', () => {
    const body = btoa(JSON.stringify({ sub: 'u1', mfa_setup: true }))
    useAuthStore.setState({ token: `h.${body}.s`, user: null })
    renderAt('/secret')
    expect(screen.getByText('Security page')).toBeInTheDocument()
    expect(screen.queryByText('Protected content')).not.toBeInTheDocument()
  })

  it('renders the children when a token is present', () => {
    useAuthStore.setState({ token: 'a-token', user: null })
    renderAt('/secret')
    expect(screen.getByText('Protected content')).toBeInTheDocument()
  })

  it('stops rendering protected content the moment the token is cleared', () => {
    // This is what a 401 from an expired token triggers.
    useAuthStore.setState({ token: 'a-token', user: null })
    const { rerender } = renderAt('/secret')
    expect(screen.getByText('Protected content')).toBeInTheDocument()

    useAuthStore.setState({ token: null, user: null })
    rerender(
      <MemoryRouter initialEntries={['/secret']}>
        <Routes>
          <Route path="/login" element={<p>Sign in page</p>} />
          <Route
            path="/secret"
            element={
              <RequireAuth>
                <p>Protected content</p>
              </RequireAuth>
            }
          />
        </Routes>
      </MemoryRouter>,
    )
    expect(screen.getByText('Sign in page')).toBeInTheDocument()
  })
})
