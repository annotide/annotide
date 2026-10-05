import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  useAddMember,
  useMembers,
  useRemoveMember,
  useServiceAccounts,
  useUpdateMember,
  useUpdateMemberFolders,
} from '@/api/queries'
import { ApiError } from '@/api/client'
import type { ProjectRole } from '@/api/types'
import { useAuthStore } from '@/lib/store'
import { BusinessBadge, useBusinessFeature } from '@/components/BusinessBadge'
import { Button } from '@/components/Button'
import { Spinner } from '@/components/Spinner'
import { ErrorState } from '@/components/ErrorState'
import { EmptyState } from '@/components/EmptyState'

const ROLE_OPTIONS: ProjectRole[] = ['owner', 'annotator', 'reviewer', 'viewer']

export interface MembersPanelProps {
  projectId: string
}

/** Project member management (SEC-3). Owner only for writes — the last owner
 * cannot be demoted or removed (409, surfaced from `ApiError.detail`).
 * Everyone else sees a read-only table. */
/** Folders as typed ("site-a/, site-b/"); applied on blur or Enter, empty = all. */
function FoldersInput({
  label,
  value,
  placeholder,
  disabled,
  onCommit,
}: {
  label: string
  value: string[] | null
  placeholder: string
  disabled: boolean
  onCommit: (pathPrefixes: string[] | null) => void
}): JSX.Element {
  const shown = (value ?? []).join(', ')
  const [text, setText] = useState(shown)
  useEffect(() => setText(shown), [shown])

  function commit(): void {
    const prefixes = text
      .split(',')
      .map((part) => part.trim())
      .filter(Boolean)
    const next = prefixes.length > 0 ? prefixes : null
    if ((next ?? []).join(', ') !== shown) onCommit(next)
  }

  return (
    <input
      aria-label={label}
      value={text}
      placeholder={placeholder}
      disabled={disabled}
      onChange={(event) => setText(event.target.value)}
      onBlur={commit}
      onKeyDown={(event) => {
        if (event.key === 'Enter') commit()
      }}
      className="w-48 rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink
        disabled:opacity-50"
    />
  )
}

export function MembersPanel({ projectId }: MembersPanelProps): JSX.Element {
  const { t } = useTranslation(['settings', 'common'])
  const membersQuery = useMembers(projectId)
  const currentUser = useAuthStore((s) => s.user)
  const addMember = useAddMember(projectId)
  const updateMember = useUpdateMember(projectId)
  const updateFolders = useUpdateMemberFolders(projectId)
  const foldersLocked = useBusinessFeature('path_permissions') === false
  const removeMember = useRemoveMember(projectId)

  const [email, setEmail] = useState('')
  const [role, setRole] = useState<ProjectRole>('annotator')

  const members = membersQuery.data ?? []
  const currentMember = members.find((m) => m.user_id === currentUser?.id)
  const isOwner = Boolean(currentUser?.is_superuser) || currentMember?.role === 'owner'
  // Listing service accounts is superuser-only (AUTH-4); an owner who is not
  // one still adds an account by pasting its svc-… e-mail.
  const isSuperuser = Boolean(currentUser?.is_superuser)
  const accountsQuery = useServiceAccounts(isSuperuser)
  const memberIds = new Set(members.map((m) => m.user_id))
  const addableAccounts = (accountsQuery.data ?? []).filter(
    (account) => account.is_active && !memberIds.has(account.id),
  )

  function handleAdd(): void {
    if (!email.trim()) return
    addMember.mutate(
      { email: email.trim(), role },
      {
        onSuccess: () => {
          setEmail('')
          setRole('annotator')
        },
      },
    )
  }

  return (
    <section aria-labelledby="members-heading" className="mb-8">
      <h2 id="members-heading" className="mb-3 text-lg font-semibold text-ink">
        {t('members.heading')}
      </h2>

      {membersQuery.isLoading && (
        <div className="flex justify-center py-6">
          <Spinner label={t('members.loading')} />
        </div>
      )}

      {membersQuery.isError && (
        <ErrorState
          title={t('members.loadError')}
          message={
            membersQuery.error instanceof ApiError
              ? (membersQuery.error.detail ?? membersQuery.error.title)
              : t('members.unknownError')
          }
          onRetry={() => void membersQuery.refetch()}
        />
      )}

      {membersQuery.data && members.length === 0 && (
        <EmptyState title={t('members.emptyTitle')} message={t('members.emptyMessage')} />
      )}

      {members.length > 0 && (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-line text-muted">
                <th className="py-1.5 pr-2 font-medium">{t('members.nameColumn')}</th>
                <th className="py-1.5 pr-2 font-medium">{t('members.emailColumn')}</th>
                <th className="py-1.5 pr-2 font-medium">{t('members.roleColumn')}</th>
                <th className="py-1.5 pr-2 font-medium">
                  {t('members.foldersColumn')}
                  {foldersLocked && (
                    <span className="ml-2">
                      <BusinessBadge linked />
                    </span>
                  )}
                </th>
                {isOwner && <th className="py-1.5 font-medium">{t('members.actionsColumn')}</th>}
              </tr>
            </thead>
            <tbody>
              {members.map((member) => (
                <tr key={member.user_id} className="border-b border-line/50">
                  <td className="py-1.5 pr-2 text-ink">{member.display_name}</td>
                  <td className="py-1.5 pr-2 text-ink">{member.email}</td>
                  <td className="py-1.5 pr-2">
                    <select
                      aria-label={`role-${member.email}`}
                      value={member.role}
                      disabled={!isOwner || updateMember.isPending}
                      onChange={(event) =>
                        updateMember.mutate({
                          userId: member.user_id,
                          role: event.target.value as ProjectRole,
                        })
                      }
                      className="rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink
                        disabled:opacity-50"
                    >
                      {ROLE_OPTIONS.map((option) => (
                        <option key={option} value={option}>
                          {t(`members.roles.${option}`)}
                        </option>
                      ))}
                    </select>
                    {member.source === 'idp' && (
                      <span
                        title={t('members.ssoGroupTitle')}
                        className="ml-2 rounded bg-line/40 px-1.5 py-0.5 text-xs text-muted"
                      >
                        {t('members.ssoGroupBadge')}
                      </span>
                    )}
                  </td>
                  <td className="py-1.5 pr-2">
                    {member.role === 'owner' || !isOwner ? (
                      <span className="text-xs text-muted">
                        {member.path_prefixes?.length
                          ? member.path_prefixes.join(', ')
                          : t('members.allFolders')}
                      </span>
                    ) : (
                      <FoldersInput
                        label={`folders-${member.email}`}
                        value={member.path_prefixes ?? null}
                        placeholder={t('members.allFolders')}
                        disabled={updateFolders.isPending || foldersLocked}
                        onCommit={(pathPrefixes) =>
                          updateFolders.mutate({ userId: member.user_id, pathPrefixes })
                        }
                      />
                    )}
                  </td>
                  {isOwner && (
                    <td className="py-1.5">
                      <Button
                        variant="danger"
                        size="sm"
                        disabled={removeMember.isPending}
                        onClick={() => removeMember.mutate(member.user_id)}
                      >
                        {t('common:remove')}
                      </Button>
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {(updateMember.isError || removeMember.isError || updateFolders.isError) && (
        <p className="mt-3 text-sm text-danger" role="alert">
          {(() => {
            const error = updateMember.error ?? removeMember.error ?? updateFolders.error
            return error instanceof ApiError ? (error.detail ?? error.title) : t('members.requestFailed')
          })()}
        </p>
      )}

      {isOwner && (
        <div className="mt-4 flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-sm">
            {t('members.emailLabel')}
            <input
              aria-label="new-member-email"
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink"
            />
          </label>
          {addableAccounts.length > 0 && (
            <label className="flex flex-col gap-1 text-sm">
              {t('members.serviceAccountLabel')}
              <select
                aria-label="new-member-service-account"
                value={addableAccounts.some((a) => a.email === email) ? email : ''}
                onChange={(event) => setEmail(event.target.value)}
                className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink"
              >
                <option value="">{t('members.pickOne')}</option>
                {addableAccounts.map((account) => (
                  <option key={account.id} value={account.email}>
                    {account.display_name}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label className="flex flex-col gap-1 text-sm">
            {t('members.roleLabel')}
            <select
              aria-label="new-member-role"
              value={role}
              onChange={(event) => setRole(event.target.value as ProjectRole)}
              className="rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink"
            >
              {ROLE_OPTIONS.map((option) => (
                <option key={option} value={option}>
                  {t(`members.roles.${option}`)}
                </option>
              ))}
            </select>
          </label>
          <Button variant="primary" disabled={addMember.isPending} onClick={handleAdd}>
            {addMember.isPending ? t('members.adding') : t('members.addMember')}
          </Button>
        </div>
      )}

      {addMember.isError && (
        <p className="mt-3 text-sm text-danger" role="alert">
          {addMember.error instanceof ApiError
            ? (addMember.error.detail ?? addMember.error.title)
            : t('members.addError')}
        </p>
      )}
    </section>
  )
}
