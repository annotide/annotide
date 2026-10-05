import { useEffect } from 'react'
import { useTranslation } from 'react-i18next'
import { matchPath, useLocation } from 'react-router-dom'

import { useProject } from '@/api/queries'

/** Route patterns → the `shell` key naming the page (UX-7, WCAG 2.4.2). */
const ROUTE_TITLES = [
  ['/', 'titles.projects'],
  ['/projects/:projectId', 'titles.project'],
  ['/projects/:projectId/dashboard', 'titles.dashboard'],
  ['/projects/:projectId/settings', 'titles.projectSettings'],
  ['/projects/:projectId/annotate/:itemId?', 'titles.annotate'],
  ['/projects/:projectId/review/:itemId?', 'titles.review'],
  ['/connectors', 'titles.connectors'],
  ['/models', 'titles.models'],
  ['/webhooks', 'titles.webhooks'],
  ['/settings/api-keys', 'titles.apiKeys'],
  ['/settings/license', 'titles.licence'],
  ['/settings/security', 'titles.security'],
  ['/settings/users', 'titles.users'],
  ['/login', 'titles.signIn'],
] as const

/** `document.title` for `pathname`'s route, most specific part first. */
export function useDocumentTitle(pathname: string): void {
  const { t } = useTranslation('shell')
  let key: (typeof ROUTE_TITLES)[number][1] | 'titles.notFound' = 'titles.notFound'
  let projectId: string | undefined
  for (const [pattern, name] of ROUTE_TITLES) {
    const match = matchPath(pattern, pathname)
    if (match) {
      key = name
      projectId = match.params.projectId
      break
    }
  }
  const project = useProject(projectId)
  const projectName = projectId ? project.data?.name : undefined

  // "Annotate · Demo project · Annotide": the page, then where. A
  // project's own page is named after the project.
  const page = key === 'titles.project' && projectName ? projectName : t(key)
  const where = key === 'titles.project' ? undefined : projectName
  const text = [page, where, t('titles.app')].filter(Boolean).join(' · ')

  useEffect(() => {
    document.title = text
  }, [text])
}

/** Sets the page title from the current route; renders nothing. */
export function DocumentTitle(): null {
  useDocumentTitle(useLocation().pathname)
  return null
}
