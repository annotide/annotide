/**
 * Project quality (QA-2, QA-4): inter-annotator agreement over consensus
 * items, per-annotator accuracy against gold references, and opening gold
 * tasks. Owners and reviewers only — the endpoints refuse anyone else.
 */

import { useTranslation } from 'react-i18next'

import { ApiError } from '@/api/client'
import { useAgreement, useAnnotatorQuality, useOpenGoldTasks } from '@/api/queries'
import { BusinessBadge, useBusinessFeature } from '@/components/BusinessBadge'
import { Button } from '@/components/Button'

function ratio(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : value.toFixed(2)
}

function problem(error: unknown, fallback: string): string {
  return error instanceof ApiError ? (error.detail ?? error.title) : fallback
}

export function QualityPanel({ projectId }: { projectId: string }): JSX.Element {
  const { t } = useTranslation('projects')
  const agreement = useAgreement(projectId)
  const quality = useAnnotatorQuality(projectId)
  const openGold = useOpenGoldTasks(projectId)
  const locked = useBusinessFeature('quality') === false

  const people = new Map(
    (agreement.data?.annotators ?? []).map((a) => [a.user_id, a.display_name ?? a.email ?? a.user_id]),
  )
  const name = (id: string) => people.get(id) ?? id.slice(0, 8)

  return (
    <section aria-labelledby="quality-heading" className="mb-8">
      <h2 id="quality-heading" className="mb-3 text-lg font-semibold text-ink">
        {t('quality.heading')}
        {locked && (
          <span className="ml-2 align-middle">
            <BusinessBadge linked />
          </span>
        )}
      </h2>

      <h3 className="mb-1 text-sm font-semibold text-ink">{t('quality.agreement.title')}</h3>
      {agreement.isError ? (
        <p role="alert" className="mb-4 text-sm text-danger">
          {problem(agreement.error, t('quality.agreement.loadError'))}
        </p>
      ) : !agreement.data ? (
        <p className="mb-4 text-sm text-muted">{t('quality.agreement.loading')}</p>
      ) : agreement.data.items === 0 ? (
        <p className="mb-4 text-sm text-muted">{t('quality.agreement.none')}</p>
      ) : (
        <div className="mb-4 space-y-2 text-sm">
          <p className="text-ink">
            {t('quality.agreement.summary', {
              items: agreement.data.items,
              shapeF1: ratio(agreement.data.shapes.f1),
              meanIou: ratio(agreement.data.shapes.mean_iou),
            })}
            {agreement.data.shapes.envelope_iou ? '*' : ''}
            {agreement.data.spans.f1_exact !== null &&
              t('quality.agreement.spanSummary', {
                exact: ratio(agreement.data.spans.f1_exact),
                overlap: ratio(agreement.data.spans.f1_overlap),
              })}
          </p>
          {agreement.data.classification.length > 0 && (
            <div className="overflow-x-auto">
              <table className="text-left text-xs">
                <thead className="text-muted">
                  <tr>
                    <th className="pr-4 font-normal">{t('quality.agreement.fields.field')}</th>
                    <th className="pr-4 font-normal">{t('quality.agreement.fields.items')}</th>
                    <th className="pr-4 font-normal">{t('quality.agreement.fields.fleiss')}</th>
                    <th className="font-normal">{t('quality.agreement.fields.krippendorff')}</th>
                  </tr>
                </thead>
                <tbody className="text-ink">
                  {agreement.data.classification.map((field) => (
                    <tr key={field.field}>
                      <td className="pr-4">{field.field}</td>
                      <td className="pr-4">{field.items}</td>
                      <td className="pr-4">{ratio(field.fleiss_kappa)}</td>
                      <td>{ratio(field.krippendorff_alpha)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {agreement.data.pairs.length > 0 && (
            <div className="overflow-x-auto">
              <table className="text-left text-xs">
                <thead className="text-muted">
                  <tr>
                    <th className="pr-4 font-normal">{t('quality.agreement.pairs.pair')}</th>
                    <th className="pr-4 font-normal">{t('quality.agreement.pairs.items')}</th>
                    <th className="pr-4 font-normal">{t('quality.agreement.pairs.cohen')}</th>
                    <th className="pr-4 font-normal">{t('quality.agreement.pairs.shapeF1')}</th>
                    <th className="font-normal">{t('quality.agreement.pairs.spanF1')}</th>
                  </tr>
                </thead>
                <tbody className="text-ink">
                  {agreement.data.pairs.map((pair) => (
                    <tr key={`${pair.a}-${pair.b}`}>
                      <td className="pr-4">
                        {name(pair.a)} · {name(pair.b)}
                      </td>
                      <td className="pr-4">{pair.items ?? '—'}</td>
                      <td className="pr-4">{ratio(pair.cohen_kappa)}</td>
                      <td className="pr-4">{ratio(pair.shape_f1)}</td>
                      <td>{ratio(pair.span_f1_exact)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {agreement.data.shapes.envelope_iou && (
            <p className="text-xs text-muted">{t('quality.agreement.envelopeNote')}</p>
          )}
        </div>
      )}

      <div className="mb-1 flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-ink">{t('quality.accuracy.title')}</h3>
        <Button
          size="sm"
          variant="secondary"
          disabled={openGold.isPending}
          onClick={() => openGold.mutate(undefined)}
          title={t('quality.accuracy.openGoldTasksTitle')}
        >
          {t('quality.accuracy.openGoldTasks')}
        </Button>
      </div>
      {openGold.isSuccess && (
        <p role="status" className="mb-2 text-xs text-muted">
          {t('quality.accuracy.opened', { count: openGold.data.opened })}
          {openGold.data.skipped > 0
            ? t('quality.accuracy.alreadyExisted', { count: openGold.data.skipped })
            : ''}
          .
        </p>
      )}
      {openGold.isError && (
        <p role="alert" className="mb-2 text-xs text-danger">
          {problem(openGold.error, t('quality.accuracy.openError'))}
        </p>
      )}
      {quality.isError ? (
        <p role="alert" className="text-sm text-danger">
          {problem(quality.error, t('quality.accuracy.loadError'))}
        </p>
      ) : !quality.data ? (
        <p className="text-sm text-muted">{t('quality.accuracy.loading')}</p>
      ) : quality.data.annotators.length === 0 ? (
        <p className="text-sm text-muted">{t('quality.accuracy.none')}</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="text-left text-xs">
            <thead className="text-muted">
              <tr>
                <th className="pr-4 font-normal">{t('quality.accuracy.columns.annotator')}</th>
                <th className="pr-4 font-normal">{t('quality.accuracy.columns.goldItems')}</th>
                <th className="pr-4 font-normal">{t('quality.accuracy.columns.classification')}</th>
                <th className="pr-4 font-normal">{t('quality.accuracy.columns.shapeF1')}</th>
                <th className="pr-4 font-normal">{t('quality.accuracy.columns.spanF1')}</th>
                <th className="font-normal">{t('quality.accuracy.columns.score')}</th>
              </tr>
            </thead>
            <tbody className="text-ink">
              {quality.data.annotators.map((row) => (
                <tr key={row.user_id}>
                  <td className="pr-4">{row.display_name ?? row.email ?? row.user_id.slice(0, 8)}</td>
                  <td className="pr-4">{row.gold_items}</td>
                  <td className="pr-4">{ratio(row.classification_accuracy)}</td>
                  <td className="pr-4">{ratio(row.shape_f1)}</td>
                  <td className="pr-4">{ratio(row.span_f1)}</td>
                  <td className="font-semibold">{ratio(row.score)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
