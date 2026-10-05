/**
 * Mark an item's approved annotation as its gold reference, or clear it
 * (QA-4). Gold tasks are then opened from the project's Quality panel.
 */

import { useTranslation } from 'react-i18next'

import { ApiError } from '@/api/client'
import { useSetGold } from '@/api/queries'
import type { Annotation, Item } from '@/api/types'
import { Button } from '@/components/Button'

export interface GoldToggleProps {
  item: Item
  /** The item's latest primary version, if any. */
  latest: Annotation | undefined
}

export function GoldToggle({ item, latest }: GoldToggleProps): JSX.Element | null {
  const { t } = useTranslation('projects')
  const setGold = useSetGold(item.project_id)
  const goldId = typeof item.meta.gold_annotation_id === 'string' ? item.meta.gold_annotation_id : null
  const canSet = latest?.status === 'approved' && (latest.kind ?? 'primary') === 'primary'
  if (!goldId && !canSet) return null

  return (
    <section aria-labelledby="gold-heading" className="mb-6">
      <h2 id="gold-heading" className="mb-2 text-sm font-semibold text-ink">
        {t('gold.heading')}
      </h2>
      <p className="mb-2 text-xs text-muted">
        {goldId ? t('gold.explainSet') : t('gold.explainUnset')}
      </p>
      {setGold.isError && (
        <p role="alert" className="mb-2 text-sm text-danger">
          {setGold.error instanceof ApiError
            ? (setGold.error.detail ?? setGold.error.title)
            : t('gold.changeError')}
        </p>
      )}
      {goldId ? (
        <Button
          size="sm"
          variant="secondary"
          disabled={setGold.isPending}
          onClick={() => setGold.mutate({ itemId: item.id, annotationId: null })}
        >
          {t('gold.remove')}
        </Button>
      ) : (
        latest && (
          <Button
            size="sm"
            variant="secondary"
            disabled={setGold.isPending}
            onClick={() => setGold.mutate({ itemId: item.id, annotationId: latest.id })}
          >
            {t('gold.use')}
          </Button>
        )
      )}
    </section>
  )
}
