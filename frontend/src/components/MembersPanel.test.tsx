import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MembersPanel } from './MembersPanel'
import { api } from '@/api/client'
import type { Member, User } from '@/api/types'
import { useAuthStore } from '@/lib/store'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      listMembers: vi.fn(),
      addMember: vi.fn(),
      updateMember: vi.fn(),
      updateMemberFolders: vi.fn(),
      removeMember: vi.fn(),
      listServiceAccounts: vi.fn(),
    },
  }
})

function makeUser(overrides: Partial<User> = {}): User {
  return {
    id: 'u1',
    organization_id: 'org1',
    email: 'owner@example.com',
    display_name: 'Owner',
    is_active: true,
    is_superuser: false,
    last_seen_at: null,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function makeMember(overrides: Partial<Member> = {}): Member {
  return {
    user_id: 'u1',
    email: 'owner@example.com',
    display_name: 'Owner',
    role: 'owner',
    created_at: '2024-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MembersPanel projectId="p1" />
    </QueryClientProvider>,
  )
}

describe('MembersPanel', () => {
  afterEach(() => {
    useAuthStore.getState().logout()
    vi.mocked(api.listMembers).mockReset()
    vi.mocked(api.addMember).mockReset()
    vi.mocked(api.updateMember).mockReset()
    vi.mocked(api.updateMemberFolders).mockReset()
    vi.mocked(api.removeMember).mockReset()
    vi.mocked(api.listServiceAccounts).mockReset()
  })

  describe('as an owner', () => {
    beforeEach(() => {
      useAuthStore.getState().login('t', makeUser())
    })

    it('adds a member by email with the chosen role', async () => {
      vi.mocked(api.listMembers).mockResolvedValue([makeMember()])
      vi.mocked(api.addMember).mockResolvedValue(
        makeMember({ user_id: 'u2', email: 'new@example.com', role: 'reviewer' }),
      )
      const user = userEvent.setup()

      renderPanel()
      await screen.findByText('Owner')

      await user.type(screen.getByLabelText('new-member-email'), 'new@example.com')
      await user.selectOptions(screen.getByLabelText('new-member-role'), 'reviewer')
      await user.click(screen.getByRole('button', { name: 'Add member' }))

      await waitFor(() => {
        expect(api.addMember).toHaveBeenCalledWith('p1', {
          email: 'new@example.com',
          role: 'reviewer',
        })
      })
    })

    it('changes a member role', async () => {
      vi.mocked(api.listMembers).mockResolvedValue([
        makeMember(),
        makeMember({ user_id: 'u2', email: 'annotator@example.com', role: 'annotator' }),
      ])
      vi.mocked(api.updateMember).mockResolvedValue(
        makeMember({ user_id: 'u2', email: 'annotator@example.com', role: 'reviewer' }),
      )
      const user = userEvent.setup()

      renderPanel()
      await screen.findByText('annotator@example.com')

      await user.selectOptions(screen.getByLabelText('role-annotator@example.com'), 'reviewer')

      await waitFor(() => {
        expect(api.updateMember).toHaveBeenCalledWith('p1', 'u2', 'reviewer')
      })
    })

    it('limits a member to folders and gives the whole project back (§4)', async () => {
      vi.mocked(api.listMembers).mockResolvedValue([
        makeMember(),
        makeMember({ user_id: 'u2', email: 'annotator@example.com', role: 'annotator' }),
      ])
      vi.mocked(api.updateMemberFolders).mockResolvedValue(makeMember({ user_id: 'u2' }))
      const user = userEvent.setup()

      renderPanel()
      const field = await screen.findByLabelText('folders-annotator@example.com')
      // After the save the list comes back with the folders, as the API does.
      vi.mocked(api.listMembers).mockResolvedValue([
        makeMember(),
        makeMember({
          user_id: 'u2',
          email: 'annotator@example.com',
          role: 'annotator',
          path_prefixes: ['site-a/', 'site-b/'],
        }),
      ])
      // The owner row shows text, not a field.
      expect(screen.queryByLabelText('folders-owner@example.com')).toBeNull()

      await user.type(field, 'site-a/, site-b/{Enter}')
      expect(api.updateMemberFolders).toHaveBeenLastCalledWith('p1', 'u2', ['site-a/', 'site-b/'])
      await waitFor(() => expect(field).toHaveValue('site-a/, site-b/'))

      await user.clear(field)
      await user.tab()
      expect(api.updateMemberFolders).toHaveBeenLastCalledWith('p1', 'u2', null)
    })

    it('removes a member', async () => {
      vi.mocked(api.listMembers).mockResolvedValue([
        makeMember(),
        makeMember({ user_id: 'u2', email: 'annotator@example.com', role: 'annotator' }),
      ])
      vi.mocked(api.removeMember).mockResolvedValue(undefined)
      const user = userEvent.setup()

      renderPanel()
      await screen.findByText('annotator@example.com')

      const removeButtons = screen.getAllByRole('button', { name: 'Remove' })
      await user.click(removeButtons[removeButtons.length - 1])

      await waitFor(() => {
        expect(api.removeMember).toHaveBeenCalledWith('p1', 'u2')
      })
    })

    it('surfaces a 409 when removing the last owner', async () => {
      const { ApiError } = await import('@/api/client')
      vi.mocked(api.listMembers).mockResolvedValue([makeMember()])
      vi.mocked(api.removeMember).mockRejectedValue(
        new ApiError({ type: 'about:blank', title: 'Conflict', status: 409, detail: 'Cannot remove the last owner' }),
      )
      const user = userEvent.setup()

      renderPanel()
      await user.click(await screen.findByRole('button', { name: 'Remove' }))

      expect(await screen.findByText('Cannot remove the last owner')).toBeInTheDocument()
    })
  })

  describe('as a non-owner', () => {
    beforeEach(() => {
      useAuthStore.getState().login('t', makeUser({ id: 'u2', display_name: 'Annotator' }))
    })

    it('hides write controls', async () => {
      vi.mocked(api.listMembers).mockResolvedValue([
        makeMember(),
        makeMember({ user_id: 'u2', display_name: 'Annotator', email: 'a@example.com', role: 'annotator' }),
      ])

      renderPanel()
      await screen.findByText('Annotator')

      expect(screen.queryByRole('button', { name: 'Add member' })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument()
      const roleSelects = screen.getAllByRole('combobox')
      for (const select of roleSelects) {
        expect(select).toBeDisabled()
      }
    })
  })

  describe('service accounts', () => {
    const svc = (overrides: Partial<User> = {}): User =>
      makeUser({
        id: 'svc1',
        email: 'svc-abc@service.invalid',
        display_name: 'Nightly export',
        is_service: true,
        ...overrides,
      })

    it('lets a superuser pick an active non-member account', async () => {
      useAuthStore.getState().login('t', makeUser({ is_superuser: true }))
      vi.mocked(api.listMembers).mockResolvedValue([
        makeMember(),
        makeMember({ user_id: 'svc2', email: 'svc-member@service.invalid', role: 'viewer' }),
      ])
      vi.mocked(api.listServiceAccounts).mockResolvedValue([
        svc(),
        svc({ id: 'svc2', email: 'svc-member@service.invalid', display_name: 'Already in' }),
        svc({ id: 'svc3', email: 'svc-old@service.invalid', display_name: 'Old', is_active: false }),
      ])
      vi.mocked(api.addMember).mockResolvedValue(
        makeMember({ user_id: 'svc1', email: 'svc-abc@service.invalid', role: 'annotator' }),
      )
      const user = userEvent.setup()

      renderPanel()
      const picker = await screen.findByLabelText('new-member-service-account')
      const options = within(picker).getAllByRole('option').map((o) => o.textContent)
      expect(options).toEqual(['Pick one…', 'Nightly export'])

      await user.selectOptions(picker, 'svc-abc@service.invalid')
      expect(screen.getByLabelText('new-member-email')).toHaveValue('svc-abc@service.invalid')
      await user.click(screen.getByRole('button', { name: 'Add member' }))

      await waitFor(() => {
        expect(api.addMember).toHaveBeenCalledWith('p1', {
          email: 'svc-abc@service.invalid',
          role: 'annotator',
        })
      })
    })

    it('does not list service accounts for an owner who is not a superuser', async () => {
      useAuthStore.getState().login('t', makeUser())
      vi.mocked(api.listMembers).mockResolvedValue([makeMember()])

      renderPanel()
      await screen.findByText('Owner')
      expect(screen.queryByLabelText('new-member-service-account')).not.toBeInTheDocument()
      expect(api.listServiceAccounts).not.toHaveBeenCalled()
    })
  })
})
