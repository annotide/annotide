import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useUpdateProject } from '@/api/queries'
import type { WorkflowConfig } from '@/api/types'
import { ApiError } from '@/api/client'
import { Button } from '@/components/Button'

interface WorkflowSettingsProps {
  projectId: string
  workflow: WorkflowConfig
  readOnly: boolean
}

const SELECT_CLASS =
  'max-w-full rounded-md border border-line bg-surface px-2 py-1.5 text-sm text-ink ' +
  'disabled:opacity-50'

const SAMPLE_RATES = [0.05, 0.1, 0.2, 0.25, 0.5]
const GOLD_EVERY = [5, 10, 20, 50, 100]

/** The project's workflow (WF-1): review, rejection routing, skipping, self-review. */
export function WorkflowSettings({ projectId, workflow, readOnly }: WorkflowSettingsProps) {
  const { t } = useTranslation(['settings', 'common'])
  const updateProject = useUpdateProject(projectId)
  const [form, setForm] = useState<WorkflowConfig>(workflow)

  // Follow the server's value after a save or a refetch; local edits win in between.
  useEffect(() => setForm(workflow), [workflow])

  const dirty =
    form.review !== workflow.review ||
    form.rejection_returns_to !== workflow.rejection_returns_to ||
    form.allow_skip !== workflow.allow_skip ||
    form.allow_self_review !== workflow.allow_self_review ||
    form.consensus_annotators !== workflow.consensus_annotators ||
    form.review_sample_rate !== workflow.review_sample_rate ||
    form.gold_every !== workflow.gold_every

  return (
    <section aria-labelledby="workflow-heading" className="mb-8">
      <h2 id="workflow-heading" className="mb-1 text-lg font-semibold text-ink">
        {t('workflow.heading')}
      </h2>
      <p className="mb-3 max-w-prose text-sm text-muted">{t('workflow.description')}</p>
      <div className="grid gap-4 sm:grid-cols-2 [&>*]:min-w-0">
        <label className="flex flex-col gap-1 text-sm text-ink">
          {t('workflow.reviewLabel')}
          <select
            aria-label="review"
            className={SELECT_CLASS}
            value={form.review}
            disabled={readOnly}
            onChange={(event) =>
              setForm((f) => ({ ...f, review: event.target.value as WorkflowConfig['review'] }))
            }
          >
            <option value="required">{t('workflow.reviewOptions.required')}</option>
            <option value="sampled">{t('workflow.reviewOptions.sampled')}</option>
            <option value="none">{t('workflow.reviewOptions.none')}</option>
          </select>
        </label>
        {form.review === 'sampled' && (
          <label className="flex flex-col gap-1 text-sm text-ink">
            {t('workflow.sampleRateLabel')}
            <select
              aria-label={t('workflow.sampleRateLabel')}
              className={SELECT_CLASS}
              value={form.review_sample_rate}
              disabled={readOnly}
              onChange={(event) =>
                setForm((f) => ({ ...f, review_sample_rate: Number(event.target.value) }))
              }
            >
              {SAMPLE_RATES.map((rate) => (
                <option key={rate} value={rate}>
                  {Math.round(rate * 100)} %
                </option>
              ))}
            </select>
            <span className="text-xs text-muted">{t('workflow.sampleRateHint')}</span>
          </label>
        )}
        <label className="flex flex-col gap-1 text-sm text-ink">
          {t('workflow.rejectionLabel')}
          <select
            aria-label={t('workflow.rejectionLabel')}
            className={SELECT_CLASS}
            value={form.rejection_returns_to}
            disabled={readOnly || form.review === 'none'}
            onChange={(event) =>
              setForm((f) => ({
                ...f,
                rejection_returns_to: event.target
                  .value as WorkflowConfig['rejection_returns_to'],
              }))
            }
          >
            <option value="same_annotator">{t('workflow.rejectionOptions.same_annotator')}</option>
            <option value="queue">{t('workflow.rejectionOptions.queue')}</option>
          </select>
        </label>
        <label className="flex items-center gap-2 text-sm text-ink">
          <input
            type="checkbox"
            checked={form.allow_skip}
            disabled={readOnly}
            onChange={(event) => setForm((f) => ({ ...f, allow_skip: event.target.checked }))}
          />
          {t('workflow.allowSkip')}
        </label>
        <label className="flex items-center gap-2 text-sm text-ink">
          <input
            type="checkbox"
            checked={form.allow_self_review}
            disabled={readOnly || form.review === 'none'}
            onChange={(event) =>
              setForm((f) => ({ ...f, allow_self_review: event.target.checked }))
            }
          />
          {t('workflow.allowSelfReview')}
        </label>
        <label className="flex flex-col gap-1 text-sm text-ink">
          {t('workflow.consensusLabel')}
          <select
            aria-label={t('workflow.consensusLabel')}
            className={SELECT_CLASS}
            value={form.consensus_annotators}
            disabled={readOnly || form.review !== 'required'}
            onChange={(event) =>
              setForm((f) => ({ ...f, consensus_annotators: Number(event.target.value) }))
            }
          >
            {Array.from({ length: 10 }, (_, i) => i + 1).map((n) => (
              <option key={n} value={n}>
                {n === 1 ? t('workflow.consensusNone') : n}
              </option>
            ))}
          </select>
          <span className="text-xs text-muted">{t('workflow.consensusHint')}</span>
        </label>
        <label className="flex flex-col gap-1 text-sm text-ink">
          {t('workflow.goldLabel')}
          <select
            aria-label={t('workflow.goldLabel')}
            className={SELECT_CLASS}
            value={form.gold_every ?? ''}
            disabled={readOnly}
            onChange={(event) =>
              setForm((f) => ({
                ...f,
                gold_every: event.target.value === '' ? null : Number(event.target.value),
              }))
            }
          >
            <option value="">{t('workflow.goldOff')}</option>
            {GOLD_EVERY.map((n) => (
              <option key={n} value={n}>
                {t('workflow.goldEvery', { n })}
              </option>
            ))}
          </select>
          <span className="text-xs text-muted">{t('workflow.goldHint')}</span>
        </label>
      </div>

      {!readOnly && (
        <div className="mt-4 flex items-center gap-3">
          <Button
            variant="primary"
            disabled={!dirty || updateProject.isPending}
            onClick={() => updateProject.mutate({ workflow: form })}
          >
            {updateProject.isPending ? t('common:saving') : t('workflow.saveButton')}
          </Button>
          {updateProject.isSuccess && !dirty && (
            <span role="status" className="text-sm text-success">
              {t('workflow.saved')}
            </span>
          )}
          {updateProject.isError && (
            <span role="alert" className="text-sm text-danger">
              {updateProject.error instanceof ApiError
                ? (updateProject.error.detail ?? updateProject.error.title)
                : t('workflow.saveError')}
            </span>
          )}
        </div>
      )}
    </section>
  )
}
