import { useDeferredValue, useState } from 'react'
import type { FormEvent } from 'react'
import { Trans, useTranslation } from 'react-i18next'
import { useEraseUser, useUsers } from '@/api/queries'
import type { User } from '@/api/types'
import { Button } from '@/components/Button'
import { EmptyState } from '@/components/EmptyState'
import { ErrorState } from '@/components/ErrorState'
import { ScimPanel } from '@/components/ScimPanel'
import { Spinner } from '@/components/Spinner'
import { INPUT_CLASS, errorMessage } from '@/components/ApiKeyParts'
import { formatDateTime } from '@/i18n'
import { useAuthStore } from '@/lib/store'

const BADGE_CLASS =
  'ml-2 inline-block rounded-full bg-line/60 px-2 py-0.5 text-xs font-medium text-muted'

/** Typed-e-mail confirmation for SEC-6 erasure; the server checks the same match. */
function EraseForm({ user, onDone }: { user: User; onDone: () => void }): JSX.Element {
  const { t } = useTranslation(['users', 'common'])
  const erase = useEraseUser()
  const [typed, setTyped] = useState('')
  const [redactComments, setRedactComments] = useState(false)
  const matches = typed.trim().toLowerCase() === user.email.toLowerCase()

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault()
    erase.mutate(
      { userId: user.id, confirm_email: typed.trim(), redact_comments: redactComments },
      { onSuccess: onDone },
    )
  }

  return (
    <form
      onSubmit={handleSubmit}
      aria-label={t('erase.formLabel', { email: user.email })}
      className="mt-2 flex flex-col gap-3 rounded-md border border-red-300 bg-red-50/40 p-3 text-sm dark:border-red-800 dark:bg-red-950/20"
    >
      <p className="text-ink">
        <Trans t={t} i18nKey="erase.explain" components={{ strong: <strong /> }} />
      </p>
      <label className="flex flex-col gap-1 text-ink">
        <span>
          <Trans
            t={t}
            i18nKey="erase.confirm"
            values={{ email: user.email }}
            components={{ code: <code className="font-mono" /> }}
          />
        </span>
        <input
          className={INPUT_CLASS}
          value={typed}
          autoComplete="off"
          onChange={(event) => setTyped(event.target.value)}
        />
      </label>
      <label className="flex items-center gap-2 text-ink">
        <input
          type="checkbox"
          checked={redactComments}
          onChange={(event) => setRedactComments(event.target.checked)}
        />
        {t('erase.redactComments')}
      </label>
      {erase.isError && (
        <p role="alert" className="text-danger">
          {t('erase.failed', { message: errorMessage(erase.error) })}
        </p>
      )}
      <div className="flex gap-2">
        <Button type="submit" size="sm" variant="danger" disabled={!matches || erase.isPending}>
          {erase.isPending ? t('erase.submitting') : t('erase.submit')}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onDone}>
          {t('common:cancel')}
        </Button>
      </div>
    </form>
  )
}

function UserRow({
  user,
  isSelf,
  erasing,
  onToggleErase,
}: {
  user: User
  isSelf: boolean
  erasing: boolean
  onToggleErase: () => void
}): JSX.Element {
  const { t } = useTranslation(['users', 'common'])
  const erased = Boolean(user.erased_at)
  return (
    <li className="border-b border-line/50 py-3" aria-label={user.email}>
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <p className="font-medium text-ink">
            {user.display_name}
            {user.is_superuser && <span className={BADGE_CLASS}>{t('badge.admin')}</span>}
            {user.erased_at && (
              <span className={BADGE_CLASS}>
                {t('badge.erased', { date: formatDateTime(user.erased_at) })}
              </span>
            )}
            {!erased && !user.is_active && (
              <span className={BADGE_CLASS}>{t('badge.inactive')}</span>
            )}
            {isSelf && <span className={BADGE_CLASS}>{t('badge.you')}</span>}
          </p>
          <p className="mt-1 flex flex-wrap gap-3 text-xs text-muted">
            <code className="font-mono">{user.email}</code>
            <span>
              {t('lastSeen', {
                when: user.last_seen_at ? formatDateTime(user.last_seen_at) : t('common:never'),
              })}
            </span>
          </p>
        </div>
        {!erased && !isSelf && (
          <Button
            size="sm"
            variant="danger"
            aria-expanded={erasing}
            aria-label={t('erase.openLabel', { email: user.email })}
            onClick={onToggleErase}
          >
            {t('erase.open')}
          </Button>
        )}
      </div>
      {erasing && <EraseForm user={user} onDone={onToggleErase} />}
    </li>
  )
}

/** Admin directory of the organisation's people, with GDPR erasure (SEC-6) and SCIM (AUTH-3). */
export function UsersPage(): JSX.Element {
  const { t } = useTranslation(['users', 'common'])
  const currentUser = useAuthStore((state) => state.user)
  const isSuperuser = currentUser?.is_superuser ?? false
  const [search, setSearch] = useState('')
  const deferredSearch = useDeferredValue(search.trim())
  const usersQuery = useUsers(deferredSearch, isSuperuser)
  const [erasingId, setErasingId] = useState<string | null>(null)
  const users = usersQuery.data ?? []

  return (
    <div className="mx-auto w-full max-w-5xl flex-1 px-4 py-8 sm:px-6">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-xl font-semibold text-ink">{t('title')}</h1>
      </div>

      {!isSuperuser && (
        <p className="text-sm text-muted">{t('adminOnly')}</p>
      )}

      {isSuperuser && (
        <>
          <ScimPanel />
          <label className="mb-4 flex max-w-sm flex-col gap-1 text-sm text-ink">
            {t('search')}
            <input
              type="search"
              className={INPUT_CLASS}
              value={search}
              placeholder={t('searchPlaceholder')}
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          <p className="mb-4 text-xs text-muted">
            {t('hint')}
          </p>
          {usersQuery.isLoading && <Spinner label={t('loading')} />}
          {usersQuery.isError && (
            <ErrorState
              title={t('loadError')}
              message={errorMessage(usersQuery.error)}
              onRetry={() => void usersQuery.refetch()}
            />
          )}
          {usersQuery.isSuccess && users.length === 0 && (
            <EmptyState title={t('noMatch')} message={t('noMatchHint')} />
          )}
          {users.length > 0 && (
            <ul aria-label={t('listLabel')}>
              {users.map((user) => (
                <UserRow
                  key={user.id}
                  user={user}
                  isSelf={user.id === currentUser?.id}
                  erasing={erasingId === user.id}
                  onToggleErase={() => setErasingId((id) => (id === user.id ? null : user.id))}
                />
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  )
}
