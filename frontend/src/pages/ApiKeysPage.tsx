import { useState } from 'react'
import { Trans, useTranslation } from 'react-i18next'
import { useApiKeys, useCreateApiKey, useRevokeApiKey } from '@/api/queries'
import type { ApiKeyCreate, ApiKeyCreated } from '@/api/types'
import { useAuthStore } from '@/lib/store'
import { Spinner } from '@/components/Spinner'
import { ErrorState } from '@/components/ErrorState'
import { EmptyState } from '@/components/EmptyState'
import { KeyForm, KeysTable, NewKeyBanner, errorMessage } from '@/components/ApiKeyParts'
import { ServiceAccountsPanel } from '@/components/ServiceAccountsPanel'

/**
 * Personal API keys (AUTH-4): mint, list and revoke the signed-in user's keys.
 * Superusers also manage the organisation's service accounts here.
 */
export function ApiKeysPage(): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const keysQuery = useApiKeys()
  const createKey = useCreateApiKey()
  const revokeKey = useRevokeApiKey()
  const isSuperuser = useAuthStore((state) => state.user?.is_superuser ?? false)
  const [created, setCreated] = useState<ApiKeyCreated | null>(null)

  const keys = keysQuery.data ?? []

  function handleCreate(payload: ApiKeyCreate): void {
    createKey.mutate(payload, { onSuccess: (key) => setCreated(key) })
  }

  return (
    <div className="mx-auto w-full max-w-5xl flex-1 px-4 py-8 sm:px-6">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-xl font-semibold text-ink">{t('apiKeys.page.title')}</h1>
      </div>
      <p className="mb-6 max-w-prose text-sm text-muted">
        <Trans
          t={t}
          i18nKey="apiKeys.page.description"
          values={{ example: '<token>' }}
          components={{ code: <code className="font-mono text-xs" /> }}
        />
      </p>

      {created && <NewKeyBanner created={created} onDismiss={() => setCreated(null)} />}

      <KeyForm
        key={created?.id ?? 'new'}
        title={t('apiKeys.page.createTitle')}
        idPrefix="api-key"
        pending={createKey.isPending}
        errorText={createKey.isError ? errorMessage(createKey.error) : null}
        onSubmit={handleCreate}
      />

      {revokeKey.isError && (
        <p role="alert" className="mb-4 text-sm text-danger">
          {t('apiKeys.page.revokeError', { message: errorMessage(revokeKey.error) })}
        </p>
      )}

      {keysQuery.isLoading && (
        <div className="flex justify-center py-16">
          <Spinner label={t('apiKeys.page.loading')} />
        </div>
      )}

      {keysQuery.isError && (
        <ErrorState
          title={t('apiKeys.page.loadError')}
          message={errorMessage(keysQuery.error)}
          onRetry={() => void keysQuery.refetch()}
        />
      )}

      {!keysQuery.isLoading && !keysQuery.isError && keys.length === 0 && (
        <EmptyState
          title={t('apiKeys.page.empty.title')}
          message={t('apiKeys.page.empty.message')}
        />
      )}

      {keys.length > 0 && (
        <KeysTable
          keys={keys}
          revokingId={revokeKey.isPending ? (revokeKey.variables ?? null) : null}
          onRevoke={(key) => revokeKey.mutate(key.id)}
        />
      )}

      {isSuperuser && <ServiceAccountsPanel />}
    </div>
  )
}
