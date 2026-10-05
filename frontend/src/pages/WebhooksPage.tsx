import { useTranslation } from 'react-i18next'
import { useAuthStore } from '@/lib/store'
import { WebhooksPanel } from '@/components/WebhooksPanel'

/** Organisation-wide webhooks (API-4): superuser only. Project owners manage
 * their project's hooks from the project settings page instead. */
export function WebhooksPage(): JSX.Element {
  const { t } = useTranslation('admin')
  const user = useAuthStore((state) => state.user)
  const isSuperuser = user?.is_superuser ?? false

  return (
    <div className="mx-auto w-full max-w-5xl flex-1 px-4 py-8 sm:px-6">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-xl font-semibold text-ink">{t('webhooks.page.title')}</h1>
      </div>

      {!isSuperuser ? (
        <p className="text-sm text-muted">{t('webhooks.page.adminOnly')}</p>
      ) : (
        <WebhooksPanel />
      )}
    </div>
  )
}
