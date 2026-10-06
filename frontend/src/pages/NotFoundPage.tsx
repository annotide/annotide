import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { Button } from '@/components/Button'

export function NotFoundPage(): JSX.Element {
  const { t } = useTranslation('shell')
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-4 p-8 text-center">
      <p className="text-2xl font-semibold text-ink">{t('notFound.title')}</p>
      <p className="max-w-prose text-sm text-muted">
        {t('notFound.body')}
      </p>
      <Link to="/">
        <Button variant="secondary">{t('notFound.back')}</Button>
      </Link>
    </div>
  )
}
