import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { useLicense } from '@/api/queries'
import type { BusinessFeature } from '@/api/types'
import { useAuthStore } from '@/lib/store'

/** A "Business" pill for a locked feature (LIC-35); links superusers to the licence page. */
export function BusinessBadge({ linked = false }: { linked?: boolean }): JSX.Element {
  const { t } = useTranslation('admin')
  const className =
    'rounded-full bg-accent/10 px-2 py-0.5 text-xs font-medium text-accent'
  if (linked) {
    return (
      <Link to="/settings/license" className={`${className} underline`} title={t('licence.badge.hint')}>
        {t('licence.badge.label')}
      </Link>
    )
  }
  return <span className={className}>{t('licence.badge.label')}</span>
}

/**
 * Whether `feature` is licensed, as far as this user can tell (LIC-33):
 * `undefined` while a superuser's licence is loading, `null` for everyone
 * else, who can't read the licence (the API's 403 `license-feature` then
 * explains a refusal).
 */
export function useBusinessFeature(feature: BusinessFeature): boolean | null | undefined {
  const isSuperuser = useAuthStore((state) => state.user?.is_superuser ?? false)
  const licence = useLicense(isSuperuser)
  if (!isSuperuser) return null
  if (!licence.data) return licence.isError ? null : undefined
  return licence.data.features.includes(feature)
}
