import { useEffect, useId, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router-dom'
import { useMarkAllRead, useMarkNotificationRead, useNotifications, useUnreadCount } from '@/api/queries'
import type { Notification } from '@/api/types'
import i18n from '@/i18n'
import { Button } from './Button'

const POLL_INTERVAL_MS = 30_000

function notificationText(notification: Notification): string {
  const { payload } = notification
  switch (notification.type) {
    case 'mention':
      return i18n.t('shell:notifications.mention', { excerpt: payload.excerpt ?? '' })
    case 'reply':
      return i18n.t('shell:notifications.reply', { excerpt: payload.excerpt ?? '' })
    case 'review':
      if (payload.approve) {
        return payload.comment
          ? i18n.t('shell:notifications.approvedWithComment', { comment: payload.comment })
          : i18n.t('shell:notifications.approved')
      }
      return payload.comment
        ? i18n.t('shell:notifications.rejectedWithComment', { comment: payload.comment })
        : i18n.t('shell:notifications.rejected')
    default:
      return i18n.t('shell:notifications.generic')
  }
}

/** Header bell (WF-5): unread badge polling every 30s, a dropdown of the
 * latest notifications, and navigation to the item a notification is about. */
export function NotificationsBell(): JSX.Element {
  const { t } = useTranslation('shell')
  const [isOpen, setIsOpen] = useState(false)
  const containerRef = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()

  const unreadCountQuery = useUnreadCount({ refetchInterval: POLL_INTERVAL_MS })
  const notificationsQuery = useNotifications({ limit: 20 })
  const markRead = useMarkNotificationRead()
  const markAllRead = useMarkAllRead()
  const buttonRef = useRef<HTMLButtonElement>(null)
  const panelId = useId()

  useEffect(() => {
    if (!isOpen) return undefined

    function handleKeyDown(event: KeyboardEvent): void {
      if (event.key !== 'Escape') return
      setIsOpen(false)
      buttonRef.current?.focus()
    }
    function handleClickOutside(event: MouseEvent): void {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setIsOpen(false)
      }
    }

    document.addEventListener('keydown', handleKeyDown)
    document.addEventListener('mousedown', handleClickOutside)
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      document.removeEventListener('mousedown', handleClickOutside)
    }
  }, [isOpen])

  const count = unreadCountQuery.data?.count ?? 0
  const notifications = notificationsQuery.data?.items ?? []

  const handleSelect = (notification: Notification): void => {
    markRead.mutate(notification.id)
    setIsOpen(false)
    navigate(`/projects/${notification.payload.project_id}/annotate/${notification.payload.item_id}`)
  }

  return (
    <div ref={containerRef} className="relative">
      <button
        ref={buttonRef}
        type="button"
        aria-label={
          count > 0 ? t('notifications.titleUnread', { count }) : t('notifications.title')
        }
        aria-expanded={isOpen}
        aria-controls={isOpen ? panelId : undefined}
        onClick={() => setIsOpen((open) => !open)}
        className="relative rounded-md border border-line p-1.5 text-ink transition-colors
          hover:bg-line/30 focus-visible:outline focus-visible:outline-2
          focus-visible:outline-offset-2 focus-visible:outline-accent"
      >
        <svg
          aria-hidden="true"
          width={18}
          height={18}
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path d="M18 8a6 6 0 10-12 0c0 7-3 9-3 9h18s-3-2-3-9" />
          <path d="M13.73 21a2 2 0 01-3.46 0" />
        </svg>
        {count > 0 && (
          <span
            className="absolute -right-1 -top-1 flex h-4 min-w-[1rem] items-center justify-center
              rounded-full bg-accent-fill px-1 text-[10px] font-semibold text-white"
          >
            {count}
          </span>
        )}
      </button>

      {isOpen && (
        <div
          id={panelId}
          className="absolute right-0 z-10 mt-2 w-80 max-w-[calc(100vw-2rem)] rounded-md border
            border-line bg-surface shadow-lg"
        >
          <div className="flex items-center justify-between border-b border-line px-3 py-2">
            <span className="text-sm font-semibold text-ink">{t('notifications.title')}</span>
            <Button variant="ghost" size="sm" className="px-1.5 py-0.5" onClick={() => markAllRead.mutate()}>
              {t('notifications.markAllRead')}
            </Button>
          </div>
          {notifications.length === 0 ? (
            <p className="p-4 text-sm text-muted">{t('notifications.empty')}</p>
          ) : (
            <ul className="max-h-96 overflow-y-auto">
              {notifications.map((notification) => (
                <li key={notification.id}>
                  <button
                    type="button"
                    onClick={() => handleSelect(notification)}
                    className={`block w-full px-3 py-2 text-left text-sm hover:bg-line/30 ${
                      notification.read_at ? 'text-muted' : 'font-semibold text-ink'
                    }`}
                  >
                    {notificationText(notification)}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
