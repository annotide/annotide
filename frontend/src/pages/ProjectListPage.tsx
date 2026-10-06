import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useCreateProject, useProjects } from '@/api/queries'
import { Spinner } from '@/components/Spinner'
import { ErrorState } from '@/components/ErrorState'
import { EmptyState } from '@/components/EmptyState'
import { Button } from '@/components/Button'
import { ApiError } from '@/api/client'

const INPUT_CLASS =
  'w-full rounded-md border border-line bg-surface px-3 py-2 text-sm text-ink ' +
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent'

function errorMessage(error: unknown, unknownError: string): string {
  return error instanceof ApiError ? (error.detail ?? error.title) : unknownError
}

/**
 * Inline "new project" form. Creating only needs a name; connectors and the
 * label schema are set on the settings page, which the form navigates to.
 */
function NewProjectForm({ onCancel }: { onCancel: () => void }): JSX.Element {
  const { t } = useTranslation(['projects', 'common'])
  const navigate = useNavigate()
  const createProject = useCreateProject()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const trimmed = name.trim()

  return (
    <form
      aria-label={t('list.form.ariaLabel')}
      className="mb-6 rounded-lg border border-line bg-surface p-4"
      onSubmit={(event) => {
        event.preventDefault()
        if (!trimmed) return
        createProject.mutate(
          { name: trimmed, description: description.trim() || null },
          { onSuccess: (project) => navigate(`/projects/${project.id}/settings`) },
        )
      }}
    >
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block text-sm text-ink">
          <span className="mb-1 block text-muted">{t('list.form.name')}</span>
          <input
            className={INPUT_CLASS}
            value={name}
            onChange={(event) => setName(event.target.value)}
            required
            autoFocus
          />
        </label>
        <label className="block text-sm text-ink">
          <span className="mb-1 block text-muted">{t('list.form.description')}</span>
          <input
            className={INPUT_CLASS}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
        </label>
      </div>
      {createProject.isError && (
        <p role="alert" className="mt-3 text-sm text-danger">
          {errorMessage(createProject.error, t('errors.unknown'))}
        </p>
      )}
      <div className="mt-4 flex gap-2">
        <Button type="submit" disabled={!trimmed || createProject.isPending}>
          {createProject.isPending ? t('list.form.creating') : t('list.form.create')}
        </Button>
        <Button type="button" variant="ghost" onClick={onCancel}>
          {t('common:cancel')}
        </Button>
      </div>
    </form>
  )
}

/** Grid of projects with loading / empty / error states and a create form. */
export function ProjectListPage(): JSX.Element {
  const { t } = useTranslation('projects')
  const { data, isLoading, isError, error, refetch } = useProjects()
  const [creating, setCreating] = useState(false)

  return (
    <div className="mx-auto w-full max-w-6xl flex-1 px-4 py-8 sm:px-6">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-xl font-semibold text-ink">{t('list.heading')}</h1>
        {!creating && (
          <Button type="button" onClick={() => setCreating(true)}>
            {t('list.newProject')}
          </Button>
        )}
      </div>

      {creating && <NewProjectForm onCancel={() => setCreating(false)} />}

      {isLoading && (
        <div className="flex justify-center py-16">
          <Spinner label={t('list.loading')} />
        </div>
      )}

      {isError && (
        <ErrorState
          title={t('list.loadError')}
          message={errorMessage(error, t('errors.unknown'))}
          onRetry={() => void refetch()}
        />
      )}

      {!isLoading && !isError && data && data.items.length === 0 && (
        <EmptyState title={t('list.empty.title')} message={t('list.empty.message')} />
      )}

      {!isLoading && !isError && data && data.items.length > 0 && (
        <ul className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {data.items.map((project) => (
            <li key={project.id}>
              <Link
                to={`/projects/${project.id}`}
                className="block h-full rounded-lg border border-line bg-surface p-4
                  transition-colors hover:border-accent focus-visible:outline
                  focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
              >
                <p className="font-medium text-ink">{project.name}</p>
                {project.description && (
                  <p className="mt-1 line-clamp-2 text-sm text-muted">{project.description}</p>
                )}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
