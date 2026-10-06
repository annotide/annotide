import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import {
  useApiKeys,
  useCreateApiKey,
  useCreateServiceAccount,
  useDeleteServiceAccount,
  useRevokeApiKey,
  useServiceAccounts,
} from '@/api/queries'
import type { ApiKeyCreate, ApiKeyCreated, User } from '@/api/types'
import { Spinner } from '@/components/Spinner'
import { ErrorState } from '@/components/ErrorState'
import { EmptyState } from '@/components/EmptyState'
import { Button } from '@/components/Button'
import {
  INPUT_CLASS,
  KeyForm,
  KeysTable,
  NewKeyBanner,
  errorMessage,
  formatDate,
} from '@/components/ApiKeyParts'

function CreateAccountForm(): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const createAccount = useCreateServiceAccount()
  const [name, setName] = useState('')

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault()
    createAccount.mutate({ display_name: name.trim() }, { onSuccess: () => setName('') })
  }

  return (
    <form
      onSubmit={handleSubmit}
      aria-label={t('serviceAccounts.createForm.ariaLabel')}
      className="mb-6 flex flex-wrap items-end gap-3"
    >
      <label className="flex flex-col gap-1 text-sm text-ink">
        {t('serviceAccounts.createForm.displayName')}
        <input
          className={INPUT_CLASS}
          value={name}
          maxLength={255}
          placeholder={t('serviceAccounts.createForm.namePlaceholder')}
          onChange={(event) => setName(event.target.value)}
          required
        />
      </label>
      <Button type="submit" disabled={name.trim() === '' || createAccount.isPending}>
        {createAccount.isPending
          ? t('serviceAccounts.createForm.creating')
          : t('serviceAccounts.createForm.submit')}
      </Button>
      {createAccount.isError && (
        <p role="alert" className="w-full text-sm text-danger">
          {t('serviceAccounts.createForm.error', { message: errorMessage(createAccount.error) })}
        </p>
      )}
    </form>
  )
}

/** Keys held by one service account: mint (via `user_id`), list, revoke. */
function AccountKeys({ account }: { account: User }): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const keysQuery = useApiKeys(account.id)
  const createKey = useCreateApiKey()
  const revokeKey = useRevokeApiKey()
  const [created, setCreated] = useState<ApiKeyCreated | null>(null)
  const keys = keysQuery.data ?? []

  function handleCreate(payload: ApiKeyCreate): void {
    createKey.mutate({ ...payload, user_id: account.id }, { onSuccess: (key) => setCreated(key) })
  }

  return (
    <div className="mt-4">
      {created && <NewKeyBanner created={created} onDismiss={() => setCreated(null)} />}
      {account.is_active && (
        <KeyForm
          key={created?.id ?? 'new'}
          title={t('serviceAccounts.accountKeys.createKeyTitle', { name: account.display_name })}
          idPrefix={`svc-${account.id}`}
          pending={createKey.isPending}
          errorText={createKey.isError ? errorMessage(createKey.error) : null}
          onSubmit={handleCreate}
        />
      )}
      {revokeKey.isError && (
        <p role="alert" className="mb-4 text-sm text-danger">
          {t('serviceAccounts.accountKeys.revokeError', { message: errorMessage(revokeKey.error) })}
        </p>
      )}
      {keysQuery.isLoading && <Spinner label={t('serviceAccounts.accountKeys.loading')} />}
      {keysQuery.isError && (
        <ErrorState
          title={t('serviceAccounts.accountKeys.loadError')}
          message={errorMessage(keysQuery.error)}
          onRetry={() => void keysQuery.refetch()}
        />
      )}
      {!keysQuery.isLoading && !keysQuery.isError && keys.length === 0 && (
        <p className="text-sm text-muted">{t('serviceAccounts.accountKeys.empty')}</p>
      )}
      {keys.length > 0 && (
        <KeysTable
          keys={keys}
          revokingId={revokeKey.isPending ? (revokeKey.variables ?? null) : null}
          onRevoke={(key) => revokeKey.mutate(key.id)}
        />
      )}
    </div>
  )
}

interface AccountCardProps {
  account: User
  expanded: boolean
  onToggle: () => void
  confirming: boolean
  deactivating: boolean
  onDeactivate: () => void
}

function AccountCard({
  account,
  expanded,
  onToggle,
  confirming,
  deactivating,
  onDeactivate,
}: AccountCardProps): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const [copied, setCopied] = useState(false)

  async function copyEmail(): Promise<void> {
    try {
      await navigator.clipboard.writeText(account.email)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  return (
    <li className="rounded-lg border border-line bg-surface p-4" aria-label={account.display_name}>
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <p className="font-medium text-ink">
            {account.display_name}
            {!account.is_active && (
              <span className="ml-2 inline-block rounded-full bg-line/60 px-2 py-0.5 text-xs font-medium text-muted">
                {t('serviceAccounts.card.deactivated')}
              </span>
            )}
          </p>
          <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
            <code className="select-all font-mono">{account.email}</code>
            <Button size="sm" variant="ghost" onClick={() => void copyEmail()}>
              {copied ? t('serviceAccounts.card.copied') : t('serviceAccounts.card.copyEmail')}
            </Button>
            <span>{t('serviceAccounts.card.created', { date: formatDate(account.created_at) })}</span>
          </p>
        </div>
        <Button
          size="sm"
          variant="secondary"
          aria-expanded={expanded}
          aria-label={
            expanded
              ? t('serviceAccounts.card.hideKeysAria', { name: account.display_name })
              : t('serviceAccounts.card.showKeysAria', { name: account.display_name })
          }
          onClick={onToggle}
        >
          {expanded ? t('serviceAccounts.card.hideKeys') : t('serviceAccounts.card.showKeys')}
        </Button>
        {account.is_active && (
          <Button
            size="sm"
            variant="danger"
            disabled={deactivating}
            aria-label={t('serviceAccounts.card.deactivateAriaLabel', { name: account.display_name })}
            onClick={onDeactivate}
          >
            {confirming
              ? t('serviceAccounts.card.confirmDeactivate')
              : t('serviceAccounts.card.deactivate')}
          </Button>
        )}
      </div>
      {confirming && (
        <p role="status" className="mt-2 text-sm text-muted">
          {t('serviceAccounts.card.confirmNotice')}
        </p>
      )}
      {expanded && <AccountKeys account={account} />}
    </li>
  )
}

/**
 * Service accounts (AUTH-4), superuser only: non-person identities that act
 * only through API keys — the identity a CI job or the SDK should use.
 */
export function ServiceAccountsPanel(): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const accountsQuery = useServiceAccounts()
  const deleteAccount = useDeleteServiceAccount()
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [confirmingId, setConfirmingId] = useState<string | null>(null)
  const accounts = accountsQuery.data ?? []

  function handleDeactivate(account: User): void {
    if (confirmingId !== account.id) {
      setConfirmingId(account.id)
      return
    }
    deleteAccount.mutate(account.id, { onSettled: () => setConfirmingId(null) })
  }

  return (
    <section aria-labelledby="service-accounts-heading" className="mt-12">
      <h2 id="service-accounts-heading" className="mb-2 text-lg font-semibold text-ink">
        {t('serviceAccounts.heading')}
      </h2>
      <p className="mb-6 max-w-prose text-sm text-muted">{t('serviceAccounts.description')}</p>

      <CreateAccountForm />

      {deleteAccount.isError && (
        <p role="alert" className="mb-4 text-sm text-danger">
          {t('serviceAccounts.deactivateError', { message: errorMessage(deleteAccount.error) })}
        </p>
      )}

      {accountsQuery.isLoading && (
        <div className="flex justify-center py-8">
          <Spinner label={t('serviceAccounts.loading')} />
        </div>
      )}

      {accountsQuery.isError && (
        <ErrorState
          title={t('serviceAccounts.loadError')}
          message={errorMessage(accountsQuery.error)}
          onRetry={() => void accountsQuery.refetch()}
        />
      )}

      {!accountsQuery.isLoading && !accountsQuery.isError && accounts.length === 0 && (
        <EmptyState
          title={t('serviceAccounts.empty.title')}
          message={t('serviceAccounts.empty.message')}
        />
      )}

      {accounts.length > 0 && (
        <ul className="flex flex-col gap-3">
          {accounts.map((account) => (
            <AccountCard
              key={account.id}
              account={account}
              expanded={expandedId === account.id}
              onToggle={() => setExpandedId(expandedId === account.id ? null : account.id)}
              confirming={confirmingId === account.id}
              deactivating={deleteAccount.isPending && deleteAccount.variables === account.id}
              onDeactivate={() => handleDeactivate(account)}
            />
          ))}
        </ul>
      )}
    </section>
  )
}
