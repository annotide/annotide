import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { User } from '@/api/types'
import { useAuthStore } from '@/lib/store'
import { Layout } from './Layout'

// All three fetch on mount; the shell's own links are what is under test here.
vi.mock('./NotificationsBell', () => ({ NotificationsBell: () => null }))
vi.mock('./DocumentTitle', () => ({ DocumentTitle: () => null }))
vi.mock('./LicenseBanner', () => ({ LicenseBanner: () => null }))

const USER: User = {
  id: 'u1',
  organization_id: 'o1',
  email: 'ann@example.com',
  display_name: 'Ann',
  is_active: true,
  is_superuser: false,
  last_seen_at: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route element={<Layout />}>
          <Route path="/" element={<p>Project list</p>} />
          <Route path="/settings/api-keys" element={<p>API keys page</p>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

describe('Layout', () => {
  beforeEach(() => {
    useAuthStore.setState({ token: 'token', user: USER })
  })

  it('links back to the project list from anywhere', async () => {
    renderAt('/settings/api-keys')
    const nav = screen.getByRole('navigation', { name: 'Main' })

    await userEvent.click(within(nav).getByRole('link', { name: 'Projects' }))

    expect(screen.getByText('Project list')).toBeInTheDocument()
  })

  it('shows admin links to superusers only', () => {
    renderAt('/')
    expect(screen.queryByRole('link', { name: 'Connectors' })).not.toBeInTheDocument()
  })

  it('groups the admin pages under one menu for superusers', async () => {
    useAuthStore.setState({ token: 'token', user: { ...USER, is_superuser: true } })
    const user = userEvent.setup()
    renderAt('/')
    const admin = screen.getByRole('button', { name: 'Admin' })
    expect(admin).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByRole('link', { name: 'Connectors' })).not.toBeInTheDocument()

    await user.click(admin)
    expect(admin).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByRole('link', { name: 'Connectors' })).toHaveAttribute('href', '/connectors')
    expect(screen.getByRole('link', { name: 'Licence' })).toBeInTheDocument()

    await user.keyboard('{Escape}')
    expect(admin).toHaveAttribute('aria-expanded', 'false')
    expect(admin).toHaveFocus()
  })

  it('keeps personal settings and sign-out under the user name', async () => {
    const user = userEvent.setup()
    renderAt('/')
    expect(screen.queryByRole('button', { name: 'Admin' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Ann' }))
    await user.click(screen.getByRole('link', { name: 'API keys' }))
    expect(screen.getByText('API keys page')).toBeInTheDocument()
    // Following a link closes the menu.
    expect(screen.queryByRole('link', { name: 'Security' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Ann' }))
    await user.click(screen.getByRole('button', { name: 'Sign out' }))
    expect(useAuthStore.getState().token).toBeNull()
  })

  it('shows no language picker while English is the only language', () => {
    renderAt('/')
    expect(screen.queryByRole('combobox', { name: 'Language' })).not.toBeInTheDocument()
  })
})
