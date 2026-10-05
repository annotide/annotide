import { useState } from 'react'
import type { FormEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { formatLength } from './measure'
import type { PixelScale } from './measure'

const INPUT_CLASS =
  'w-20 rounded border border-line bg-surface px-1 py-0.5 text-xs text-ink ' +
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent'

// Resolved at render time via the `ruler.source` namespace so a language
// switch applies.
const SOURCE_KEY: Record<PixelScale['source'], 'item' | 'project' | 'session' | 'pixels'> = {
  item: 'item',
  project: 'project',
  session: 'session',
  pixels: 'pixels',
}

/**
 * The ruler's reading, and — when the caller supports it — calibration:
 * the person types the true length of what they just measured (TOOL-8).
 */
export function RulerPanel({
  pixels,
  measured,
  scale,
  onCalibrate,
}: {
  /** The ruler's length in image pixels. */
  pixels: number
  /** The same length in `scale.unit`, measured per axis. */
  measured: number
  scale: PixelScale
  onCalibrate?: (unitsPerPixel: number, unit: string) => void
}): JSX.Element {
  const { t } = useTranslation('annotator')
  const [known, setKnown] = useState('')
  const [unit, setUnit] = useState(scale.unit === 'px' ? 'mm' : scale.unit)
  const knownValue = Number(known)
  const canCalibrate = pixels > 0 && Number.isFinite(knownValue) && knownValue > 0 && unit !== ''

  const submit = (event: FormEvent): void => {
    event.preventDefault()
    if (onCalibrate && canCalibrate) onCalibrate(knownValue / pixels, unit.trim())
    setKnown('')
  }

  return (
    <div
      role="region"
      aria-label={t('ruler.ariaLabel')}
      className="absolute right-2 top-2 z-10 rounded bg-surface/95 p-2 text-xs text-ink shadow"
    >
      <p>
        <span className="font-medium">{formatLength(measured, scale)}</span>{' '}
        <span className="text-muted">
          ({Math.round(pixels)} px, {t(`ruler.source.${SOURCE_KEY[scale.source]}`)})
        </span>
      </p>
      {onCalibrate && (
        <form onSubmit={submit} className="mt-2 flex items-center gap-1">
          <label className="flex items-center gap-1">
            {t('ruler.trueLength')}
            <input
              className={INPUT_CLASS}
              inputMode="decimal"
              value={known}
              onChange={(event) => setKnown(event.target.value)}
            />
          </label>
          <label className="sr-only" htmlFor="ruler-unit">
            {t('ruler.unit')}
          </label>
          <input
            id="ruler-unit"
            className={`${INPUT_CLASS} w-12`}
            maxLength={16}
            value={unit}
            onChange={(event) => setUnit(event.target.value)}
          />
          <button
            type="submit"
            disabled={!canCalibrate}
            className="rounded border border-line px-2 py-0.5 hover:bg-line/30 disabled:opacity-50"
          >
            {t('ruler.setScale')}
          </button>
        </form>
      )}
    </div>
  )
}
