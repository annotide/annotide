/**
 * Consensus review (QA-1 … QA-3): the N independent versions of an item, how
 * far they agree, and the two ways to turn them into the item's annotation —
 * fuse them, or approve one annotator's version as it is. Resolving is the
 * approval; it goes through the same verdict path as a normal review.
 */

import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '@/api/client'
import { useResolveConsensus } from '@/api/queries'
import type { AnnotationResult, ConsensusView } from '@/api/types'
import { Button } from '@/components/Button'

export interface ConsensusPanelProps {
  itemId: string
  projectId: string
  view: ConsensusView
  /** Which result the canvas shows: an annotator's version id, or 'fused'. */
  shown: string
  onShow: (key: string, result: AnnotationResult) => void
  onResolved: () => void
  /** Versions' results by annotation id, for "Show". */
  results: Record<string, AnnotationResult>
}

function ratio(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toFixed(2)
}

export function ConsensusPanel({
  itemId,
  projectId,
  view,
  shown,
  onShow,
  onResolved,
  results,
}: ConsensusPanelProps): JSX.Element {
  const { t } = useTranslation('projects')
  const resolve = useResolveConsensus(itemId, projectId)
  const [comment, setComment] = useState('')
  const { agreement } = view

  const submitted = view.annotators.filter((a) => a.status === 'submitted')

  return (
    <section aria-labelledby="consensus-heading" className="mb-6">
      <h2 id="consensus-heading" className="mb-2 text-sm font-semibold text-ink">
        {t('consensus.heading', { count: view.annotators.length, expected: view.expected })}
      </h2>

      <dl className="mb-3 grid grid-cols-2 gap-x-2 gap-y-1 text-xs">
        <dt className="text-muted">{t('consensus.shapeF1')}</dt>
        <dd className="text-right text-ink">{ratio(agreement.shapes.f1)}</dd>
        <dt className="text-muted">
          {t('consensus.meanIou')}
          {agreement.shapes.envelope_iou ? '*' : ''}
        </dt>
        <dd className="text-right text-ink">{ratio(agreement.shapes.mean_iou)}</dd>
        {(agreement.spans.f1_exact !== null || agreement.spans.f1_overlap !== null) && (
          <>
            <dt className="text-muted">{t('consensus.spanF1')}</dt>
            <dd className="text-right text-ink">
              {ratio(agreement.spans.f1_exact)} / {ratio(agreement.spans.f1_overlap)}
            </dd>
          </>
        )}
        {agreement.classification.map((field) => (
          <div key={field.field} className="contents">
            <dt className="text-muted">{field.field} α</dt>
            <dd className="text-right text-ink">{ratio(field.krippendorff_alpha)}</dd>
          </div>
        ))}
      </dl>
      {agreement.shapes.envelope_iou && (
        <p className="mb-3 text-xs text-muted">{t('consensus.envelopeNote')}</p>
      )}

      <ul className="mb-3 space-y-1 text-sm">
        <li
          className={`flex items-center justify-between gap-2 rounded px-2 py-1 ${
            shown === 'fused' ? 'bg-line/40' : ''
          }`}
        >
          <span className="text-ink">{t('consensus.fusedPreview')}</span>
          <Button size="sm" variant="secondary" onClick={() => onShow('fused', view.preview)}>
            {t('consensus.show')}
          </Button>
        </li>
        {view.annotators.map((annotator) => {
          const result = results[annotator.annotation_id]
          return (
            <li
              key={annotator.annotation_id}
              className={`rounded px-2 py-1 ${
                shown === annotator.annotation_id ? 'bg-line/40' : ''
              }`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-ink">
                  {annotator.display_name ?? annotator.email ?? annotator.user_id}
                </span>
                <span className="text-xs text-muted">v{annotator.version}</span>
              </div>
              <div className="mt-1 flex gap-2">
                {result && (
                  <Button
                    size="sm"
                    variant="secondary"
                    onClick={() => onShow(annotator.annotation_id, result)}
                  >
                    {t('consensus.show')}
                  </Button>
                )}
                {annotator.status === 'submitted' && (
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={resolve.isPending}
                    onClick={() =>
                      resolve.mutate(
                        {
                          method: 'pick',
                          annotation_id: annotator.annotation_id,
                          comment: comment || undefined,
                        },
                        { onSuccess: onResolved },
                      )
                    }
                  >
                    {t('consensus.approveVersion')}
                  </Button>
                )}
              </div>
            </li>
          )
        })}
      </ul>

      {view.conflicts.length > 0 && (
        <div className="mb-3 text-xs">
          <p className="text-muted">{t('consensus.conflicts')}</p>
          <ul className="list-inside list-disc text-ink">
            {view.conflicts.map((conflict) => (
              <li key={conflict}>{conflict}</li>
            ))}
          </ul>
        </div>
      )}

      <label htmlFor="consensus-comment" className="mb-1 block text-sm font-medium text-ink">
        {t('consensus.comment')}
      </label>
      <textarea
        id="consensus-comment"
        value={comment}
        onChange={(event) => setComment(event.target.value)}
        rows={2}
        className="mb-2 w-full rounded-md border border-line bg-surface p-2 text-sm text-ink"
      />

      {resolve.isError && (
        <p role="alert" className="mb-2 text-sm text-danger">
          {resolve.error instanceof ApiError
            ? (resolve.error.detail ?? resolve.error.title)
            : t('consensus.resolveError')}
        </p>
      )}
      <Button
        variant="primary"
        disabled={resolve.isPending || submitted.length === 0}
        onClick={() =>
          resolve.mutate(
            { method: 'fuse', comment: comment || undefined },
            { onSuccess: onResolved },
          )
        }
      >
        {t('consensus.fuseAndApprove')}
      </Button>
    </section>
  )
}
