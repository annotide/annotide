import { Link } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { useLicense } from '@/api/queries'
import type { LicenseInfo } from '@/api/types'
import { useAuthStore } from '@/lib/store'
import i18n, { formatDateOnly } from '@/i18n'

export interface LicenceNotice {
  tone: 'warning' | 'danger'
  text: string
}

function formatDay(value: string): string {
  return formatDateOnly(`${value}T00:00:00Z`)
}

/** Admins hear about the end of a trial in its last week (LIC-34). */
const TRIAL_WARNING_DAYS = 7

function daysUntil(value: string, now: Date): number {
  const end = Date.parse(`${value}T23:59:59Z`)
  return Math.ceil((end - now.getTime()) / 86_400_000)
}

/**
 * What an administrator should be told about the licence, if anything
 * (LIC-24, LIC-5). Seat overage is a banner and an invoice, never a lockout
 * of users who are already active.
 */
export function licenceNotice(info: LicenseInfo, now: Date = new Date()): LicenceNotice | null {
  const t = i18n.t.bind(i18n)
  if (info.owner_only) {
    return { tone: 'danger', text: t('admin:licence.banner.ownerOnly') }
  }
  if (info.tier === 'trial' && info.status === 'valid' && info.expires_at) {
    const left = daysUntil(info.expires_at, now)
    if (left <= TRIAL_WARNING_DAYS) {
      return {
        tone: 'warning',
        text: t('admin:licence.banner.trialEnding', { date: formatDay(info.expires_at) }),
      }
    }
  }
  if (info.status === 'expired' && info.expires_at) {
    const expired = info.revoked_at
      ? t('admin:licence.banner.expiredRevoked', { date: formatDay(info.revoked_at) })
      : info.host_mismatch
        ? t('admin:licence.banner.expiredHostMismatch', { hosts: info.hosts.join(', ') })
        : t('admin:licence.banner.expiredPlain', { date: formatDay(info.expires_at) })
    if (info.restricted) {
      return {
        tone: 'danger',
        text: t('admin:licence.banner.restricted', { expired }),
      }
    }
    const until = info.grace_ends_at
      ? t('admin:licence.banner.untilDate', { date: formatDay(info.grace_ends_at) })
      : ''
    return {
      tone: 'warning',
      text: t('admin:licence.banner.graceWarning', { expired, until }),
    }
  }
  if (info.status === 'invalid') {
    return {
      tone: 'danger',
      text: t('admin:licence.banner.invalid'),
    }
  }
  const { seats, seat_limit: limit, active_users: active } = info
  if (seats === null || limit === null) return null
  if (active >= limit) {
    return {
      tone: 'danger',
      text: t('admin:licence.banner.allSeatsUsed', { active, seats }),
    }
  }
  if (active > seats) {
    return {
      tone: 'warning',
      text: t('admin:licence.banner.overageAllowed', { active, seats, limit }),
    }
  }
  return null
}

const TONE_CLASS: Record<LicenceNotice['tone'], string> = {
  warning: 'border-amber-500/40 bg-amber-500/10 text-amber-800 dark:text-amber-300',
  danger: 'border-red-500/40 bg-red-500/10 text-red-800 dark:text-red-300',
}

/** Licence and seat notice under the top bar; administrators only. */
export function LicenseBanner(): JSX.Element | null {
  const { t } = useTranslation('admin')
  const isSuperuser = useAuthStore((state) => state.user?.is_superuser ?? false)
  const licence = useLicense(isSuperuser)
  const notice = isSuperuser && licence.data ? licenceNotice(licence.data) : null
  if (!notice) return null

  return (
    <div
      role="status"
      className={`flex flex-wrap items-center justify-between gap-2 border-b px-4 py-2 text-sm
        sm:px-6 ${TONE_CLASS[notice.tone]}`}
    >
      <span>{notice.text}</span>
      <Link
        to="/settings/license"
        className="font-medium underline focus-visible:outline focus-visible:outline-2
          focus-visible:outline-offset-2 focus-visible:outline-accent"
      >
        {t('licence.banner.link')}
      </Link>
    </div>
  )
}
