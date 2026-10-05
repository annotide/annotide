import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMintScimToken, useRevokeScimToken, useScimToken } from '@/api/queries'
import { BusinessBadge, useBusinessFeature } from '@/components/BusinessBadge'
import { Button } from '@/components/Button'
import { ErrorState } from '@/components/ErrorState'
import { errorMessage } from '@/components/ApiKeyParts'

/** The SCIM tenant URL and a just-minted token, shown once (AUTH-3). */
function ScimTokenReveal({
  baseUrl,
  token,
  onDismiss,
}: {
  baseUrl: string
  token: string
  onDismiss: () => void
}): JSX.Element {
  const { t } = useTranslation('users')
  const [shown, setShown] = useState(false)
  return (
    <div
      role="status"
      className="mt-3 rounded-md border border-amber-400/60 bg-amber-500/10 p-3 text-sm text-ink"
    >
      <p className="mb-2 font-medium">{t('scim.revealTitle')}</p>
      <p className="text-xs text-muted">{t('scim.baseUrl')}</p>
      <code className="mb-2 block break-all font-mono text-xs" data-testid="scim-base-url">
        {baseUrl}
      </code>
      <p className="text-xs text-muted">{t('scim.token')}</p>
      <code className="block break-all font-mono text-xs" data-testid="scim-token">
        {shown ? token : '•'.repeat(24)}
      </code>
      <div className="mt-2 flex flex-wrap gap-2">
        <Button variant="secondary" size="sm" onClick={() => setShown((v) => !v)}>
          {shown ? t('scim.hide') : t('scim.reveal')}
        </Button>
        <Button
          variant="secondary"
          size="sm"
          onClick={() => {
            void navigator.clipboard?.writeText(token)
          }}
        >
          {t('scim.copyToken')}
        </Button>
        <Button variant="ghost" size="sm" onClick={onDismiss}>
          {t('scim.done')}
        </Button>
      </div>
    </div>
  )
}

/** Turn SCIM provisioning on or off and mint its token (AUTH-3, superuser). */
export function ScimPanel(): JSX.Element {
  const { t } = useTranslation('users')
  const state = useScimToken()
  const mint = useMintScimToken()
  const revoke = useRevokeScimToken()
  const [minted, setMinted] = useState<{ baseUrl: string; token: string } | null>(null)
  const enabled = state.data?.enabled ?? false
  const locked = useBusinessFeature('scim') === false
  const pending = mint.isPending || revoke.isPending
  const failure = mint.error ?? revoke.error

  function mintToken(): void {
    mint.mutate(undefined, {
      onSuccess: ({ path, token }) =>
        setMinted({ baseUrl: `${window.location.origin}${path}`, token }),
    })
  }

  return (
    <section aria-labelledby="scim-heading" className="mb-8 rounded-md border border-line p-4">
      <h2 id="scim-heading" className="mb-1 text-lg font-semibold text-ink">
        {t('scim.heading')}
        {state.isSuccess && (
          <span className="ml-2 rounded bg-line/40 px-1.5 py-0.5 align-middle text-xs font-normal text-muted">
            {enabled ? t('scim.on') : t('scim.off')}
          </span>
        )}
        {locked && (
          <span className="ml-2 align-middle">
            <BusinessBadge linked />
          </span>
        )}
      </h2>
      <p className="mb-3 max-w-prose text-sm text-muted">{t('scim.description')}</p>
      {state.isError && <ErrorState message={errorMessage(state.error)} />}
      {state.isSuccess && (
        <div className="flex flex-wrap gap-2">
          <Button variant={enabled ? 'secondary' : 'primary'} disabled={pending || locked} onClick={mintToken}>
            {enabled ? t('scim.newToken') : t('scim.enable')}
          </Button>
          {enabled && (
            <Button
              variant="danger"
              disabled={pending}
              onClick={() => {
                setMinted(null)
                revoke.mutate()
              }}
            >
              {t('scim.disable')}
            </Button>
          )}
        </div>
      )}
      {enabled && !minted && state.isSuccess && (
        <p className="mt-2 text-xs text-muted">{t('scim.newTokenHint')}</p>
      )}
      {failure && (
        <p role="alert" className="mt-2 text-sm text-danger">
          {t('scim.failed', { message: errorMessage(failure) })}
        </p>
      )}
      {minted && (
        <ScimTokenReveal
          baseUrl={minted.baseUrl}
          token={minted.token}
          onDismiss={() => setMinted(null)}
        />
      )}
    </section>
  )
}
