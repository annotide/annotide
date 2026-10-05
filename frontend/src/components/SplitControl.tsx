/**
 * Split the selected image items into a grid of region tasks (IMG-6), so
 * several annotators can work on one large image at once. Each item is split
 * on its own; the server refuses items that cannot be split (not an image, no
 * size, already split or in progress, consensus projects) and says why.
 */

import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { ApiError } from '@/api/client'
import { useSplitItem } from '@/api/queries'
import type { Item } from '@/api/types'
import { Button } from '@/components/Button'

const GRID_SIZES = Array.from({ length: 16 }, (_, i) => i + 1)
const INPUT_CLASS = 'w-16 rounded border border-line bg-surface px-2 py-1 text-sm text-ink'

export interface SplitControlProps {
  projectId: string
  items: Item[]
  onDone?: () => void
}

export function SplitControl({ projectId, items, onDone }: SplitControlProps): JSX.Element | null {
  const { t } = useTranslation('settings')
  const split = useSplitItem(projectId)
  const [rows, setRows] = useState(2)
  const [cols, setCols] = useState(2)
  const [overlap, setOverlap] = useState(0)
  const [running, setRunning] = useState(false)
  const [outcome, setOutcome] = useState<{ done: number; refused: string[] } | null>(null)

  const images = items.filter((item) => item.media_type === 'image')
  if (images.length === 0) return null

  const run = async () => {
    setRunning(true)
    setOutcome(null)
    let done = 0
    const refused: string[] = []
    for (const item of images) {
      try {
        await split.mutateAsync({
          itemId: item.id,
          body: { grid: { rows, cols, overlap_px: overlap } },
        })
        done += 1
      } catch (error) {
        const why = error instanceof ApiError ? (error.detail ?? error.title) : t('split.failed')
        refused.push(`${item.path}: ${why}`)
      }
    }
    setRunning(false)
    setOutcome({ done, refused })
    if (done > 0) onDone?.()
  }

  return (
    <div className="mt-2 flex flex-wrap items-center gap-2 text-sm text-ink" aria-label={t('split.ariaLabel')}>
      <span>{t('split.into', { count: images.length })}</span>
      <select
        aria-label={t('split.rowsAriaLabel')}
        className={INPUT_CLASS}
        value={rows}
        onChange={(event) => setRows(Number(event.target.value))}
      >
        {GRID_SIZES.map((n) => (
          <option key={n} value={n}>
            {n}
          </option>
        ))}
      </select>
      ×
      <select
        aria-label={t('split.columnsAriaLabel')}
        className={INPUT_CLASS}
        value={cols}
        onChange={(event) => setCols(Number(event.target.value))}
      >
        {GRID_SIZES.map((n) => (
          <option key={n} value={n}>
            {n}
          </option>
        ))}
      </select>
      <label className="flex items-center gap-1 text-muted">
        {t('split.overlap')}
        <input
          aria-label={t('split.overlapAriaLabel')}
          type="number"
          min={0}
          className={INPUT_CLASS}
          value={overlap}
          onChange={(event) => setOverlap(Math.max(0, Number(event.target.value) || 0))}
        />
        {t('split.px')}
      </label>
      <Button size="sm" variant="secondary" disabled={running} onClick={() => void run()}>
        {running ? t('split.splitting') : t('split.splitButton')}
      </Button>
      {outcome && (
        <div role="status" className="w-full text-xs text-muted">
          {t('split.result', { count: outcome.done })}
          {outcome.refused.length > 0 && (
            <ul className="list-inside list-disc text-danger">
              {outcome.refused.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
