import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useUpdateProject } from '@/api/queries'
import type { ProjectRole } from '@/api/types'
import { ApiError } from '@/api/client'
import { Button } from '@/components/Button'

interface IdpGroupsSettingsProps {
  projectId: string
  settings: Record<string, unknown>
  readOnly: boolean
}

interface Row {
  group: string
  role: ProjectRole
}

const ROLES: ProjectRole[] = ['viewer', 'annotator', 'reviewer', 'owner']

const INPUT_CLASS =
  'rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink disabled:opacity-50'

function rowsFrom(settings: Record<string, unknown>): Row[] {
  const mapping = settings.idp_groups
  if (!mapping || typeof mapping !== 'object' || Array.isArray(mapping)) return []
  return Object.entries(mapping as Record<string, unknown>)
    .filter((entry): entry is [string, ProjectRole] =>
      ROLES.includes(entry[1] as ProjectRole),
    )
    .map(([group, role]) => ({ group, role }))
}

function mappingOf(rows: Row[]): Record<string, ProjectRole> {
  return Object.fromEntries(
    rows.filter((row) => row.group.trim() !== '').map((row) => [row.group.trim(), row.role]),
  )
}

/** `settings.idp_groups` (AUTH-3): which SSO groups grant which role on this project. */
export function IdpGroupsSettings({ projectId, settings, readOnly }: IdpGroupsSettingsProps) {
  const { t } = useTranslation(['settings', 'common'])
  const updateProject = useUpdateProject(projectId)
  const [rows, setRows] = useState<Row[]>(() => rowsFrom(settings))

  useEffect(() => setRows(rowsFrom(settings)), [settings])

  const saved = JSON.stringify(mappingOf(rowsFrom(settings)))
  const dirty = JSON.stringify(mappingOf(rows)) !== saved

  function update(index: number, patch: Partial<Row>): void {
    setRows((current) => current.map((row, i) => (i === index ? { ...row, ...patch } : row)))
  }

  return (
    <section aria-labelledby="idp-groups-heading" className="mb-8">
      <h2 id="idp-groups-heading" className="mb-1 text-lg font-semibold text-ink">
        {t('idpGroups.heading')}
      </h2>
      <p className="mb-3 max-w-prose text-sm text-muted">{t('idpGroups.description')}</p>
      {rows.length === 0 && <p className="mb-3 text-sm text-muted">{t('idpGroups.noGroups')}</p>}
      <ul className="flex flex-col gap-2">
        {rows.map((row, index) => (
          <li key={index} className="flex flex-wrap items-center gap-2">
            <input
              aria-label={`group-${index}`}
              className={`${INPUT_CLASS} min-w-0 flex-1`}
              placeholder={t('idpGroups.groupPlaceholder')}
              value={row.group}
              maxLength={255}
              disabled={readOnly}
              onChange={(event) => update(index, { group: event.target.value })}
            />
            <select
              aria-label={`group-role-${index}`}
              className={INPUT_CLASS}
              value={row.role}
              disabled={readOnly}
              onChange={(event) => update(index, { role: event.target.value as ProjectRole })}
            >
              {ROLES.map((role) => (
                <option key={role} value={role}>
                  {t(`idpGroups.roles.${role}`)}
                </option>
              ))}
            </select>
            {!readOnly && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setRows((current) => current.filter((_, i) => i !== index))}
              >
                {t('common:remove')}
              </Button>
            )}
          </li>
        ))}
      </ul>

      {!readOnly && (
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <Button
            variant="secondary"
            onClick={() => setRows((current) => [...current, { group: '', role: 'annotator' }])}
          >
            {t('idpGroups.addGroup')}
          </Button>
          <Button
            variant="primary"
            disabled={!dirty || updateProject.isPending}
            onClick={() =>
              updateProject.mutate({ settings: { ...settings, idp_groups: mappingOf(rows) } })
            }
          >
            {updateProject.isPending ? t('common:saving') : t('idpGroups.saveButton')}
          </Button>
          {updateProject.isSuccess && !dirty && (
            <span role="status" className="text-sm text-success">
              {t('idpGroups.saved')}
            </span>
          )}
          {updateProject.isError && (
            <span role="alert" className="text-sm text-danger">
              {updateProject.error instanceof ApiError
                ? (updateProject.error.detail ?? updateProject.error.title)
                : t('idpGroups.saveError')}
            </span>
          )}
        </div>
      )}
    </section>
  )
}
