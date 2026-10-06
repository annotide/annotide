import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthCallbackPage, parseCallbackFragment } from './AuthCallbackPage'
import { api } from '@/api/client'
import type { User } from '@/api/types'
import { useAuthStore } from '@/lib/store'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      me: vi.fn(),
    },
  }
})

const user: User = {
  id: 'u1',
  organization_id: 'org1',
  email: 'person@example.com',
  display_name: 'Person',
  is_active: true,
  is_superuser: false,
  last_seen_at: null,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
} as User

function renderAt(url: string) {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/auth/callback" element={<AuthCallbackPage />} />
        <Route path="/login" element={<p>login page</p>} />
        <Route path="*" element={<p>landed</p>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('parseCallbackFragment', () => {
  it('reads the token and keeps next on-site', () => {
    expect(
      parseCallbackFragment('#access_token=abc&expires_in=3600&next=%2Fprojects%2Fp1'),
    ).toEqual({ token: 'abc', next: '/projects/p1' })
    expect(parseCallbackFragment('#next=//evil.example').next).toBe('/')
    expect(parseCallbackFragment('').token).toBeNull()
  })
})

describe('AuthCallbackPage', () => {
  beforeEach(() => {
    useAuthStore.getState().logout()
  })
  afterEach(() => {
    vi.mocked(api.me).mockReset()
  })

  it('stores the token, loads the profile and continues to next', async () => {
    vi.mocked(api.me).mockResolvedValue(user)
    renderAt('/auth/callback#access_token=tok&expires_in=3600&next=%2Fprojects%2Fp1')
    await waitFor(() => expect(screen.getByText('landed')).toBeInTheDocument())
    expect(api.me).toHaveBeenCalledWith('tok')
    expect(useAuthStore.getState().token).toBe('tok')
    expect(useAuthStore.getState().user?.email).toBe('person@example.com')
  })

  it('shows a way back when the fragment carries no token', async () => {
    renderAt('/auth/callback')
    expect(await screen.findByRole('alert')).toHaveTextContent(/could not be completed/)
    expect(screen.getByRole('link', { name: 'Back to sign in' })).toHaveAttribute('href', '/login')
    expect(api.me).not.toHaveBeenCalled()
  })

  it('fails safely when the token is rejected', async () => {
    vi.mocked(api.me).mockRejectedValue(new Error('401'))
    renderAt('/auth/callback#access_token=bad')
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(useAuthStore.getState().token).toBeNull()
  })
})
