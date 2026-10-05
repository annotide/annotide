import { useId } from 'react'
import { useTranslation } from 'react-i18next'
import { create } from 'zustand'
import {
  brightnessOf,
  channelMatrix,
  contrastOf,
  isNeutral,
  MAX_WINDOW,
  MIN_WINDOW,
  NEUTRAL,
  transfer,
  withBrightness,
  withContrast,
} from './adjustment'
import type { Channel, ImageAdjustment } from './adjustment'

/**
 * The adjustment in use, kept while stepping through items so a series is
 * read with the same window / level. In memory only.
 */
export const useImageAdjustStore = create<{
  adjustment: ImageAdjustment
  setAdjustment: (adjustment: ImageAdjustment) => void
}>((set) => ({
  adjustment: NEUTRAL,
  setAdjustment: (adjustment) => set({ adjustment }),
}))

/** The SVG filter the image layer's canvas points at (`filter: url(#id)`). */
export function ImageAdjustFilter({
  id,
  adjustment,
}: {
  id: string
  adjustment: ImageAdjustment
}): JSX.Element {
  const { slope, intercept } = transfer(adjustment)
  return (
    <svg aria-hidden="true" width="0" height="0" style={{ position: 'absolute' }}>
      {/* sRGB, not the default linearRGB: the numbers are display values. */}
      <filter id={id} colorInterpolationFilters="sRGB">
        <feColorMatrix type="matrix" values={channelMatrix(adjustment.channel)} />
        <feComponentTransfer>
          <feFuncR type="linear" slope={slope} intercept={intercept} />
          <feFuncG type="linear" slope={slope} intercept={intercept} />
          <feFuncB type="linear" slope={slope} intercept={intercept} />
        </feComponentTransfer>
      </filter>
    </svg>
  )
}

// Labels are resolved at render time from `adjust.channels` so a language
// switch applies.
const CHANNEL_KEYS = [
  { value: 'rgb', key: 'rgb' },
  { value: 'gray', key: 'gray' },
  { value: 'r', key: 'r' },
  { value: 'g', key: 'g' },
  { value: 'b', key: 'b' },
] as const satisfies ReadonlyArray<{ value: Channel; key: string }>

const NUMBER_CLASS =
  'w-16 rounded border border-line bg-surface px-1 py-0.5 text-xs text-ink ' +
  'focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent'

/** Brightness, contrast, window / level and channel controls (IMG-5). */
export function ImageAdjustPanel({
  value,
  onChange,
}: {
  value: ImageAdjustment
  onChange: (next: ImageAdjustment) => void
}): JSX.Element {
  const { t } = useTranslation('annotator')
  const base = useId()
  const number = (raw: string, fallback: number): number => {
    const parsed = Number(raw)
    return Number.isFinite(parsed) ? parsed : fallback
  }
  return (
    <div
      role="group"
      aria-label={t('adjust.ariaLabel')}
      className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line px-3 py-2 text-xs text-ink"
    >
      <label htmlFor={`${base}-b`} className="flex items-center gap-2">
        {t('adjust.brightness')}
        <input
          id={`${base}-b`}
          type="range"
          min={-100}
          max={100}
          value={brightnessOf(value)}
          onChange={(event) => onChange(withBrightness(value, Number(event.target.value)))}
        />
      </label>
      <label htmlFor={`${base}-c`} className="flex items-center gap-2">
        {t('adjust.contrast')}
        <input
          id={`${base}-c`}
          type="range"
          min={-100}
          max={100}
          value={contrastOf(value)}
          onChange={(event) => onChange(withContrast(value, Number(event.target.value)))}
        />
      </label>
      <label htmlFor={`${base}-w`} className="flex items-center gap-1">
        {t('adjust.window')}
        <input
          id={`${base}-w`}
          type="number"
          min={MIN_WINDOW}
          max={MAX_WINDOW}
          step="any"
          className={NUMBER_CLASS}
          value={Math.round(value.window)}
          onChange={(event) =>
            onChange({ ...value, window: number(event.target.value, value.window) })
          }
        />
      </label>
      <label htmlFor={`${base}-l`} className="flex items-center gap-1">
        {t('adjust.level')}
        <input
          id={`${base}-l`}
          type="number"
          min={0}
          max={255}
          step="any"
          className={NUMBER_CLASS}
          value={Math.round(value.level)}
          onChange={(event) =>
            onChange({ ...value, level: number(event.target.value, value.level) })
          }
        />
      </label>
      <label htmlFor={`${base}-ch`} className="flex items-center gap-1">
        {t('adjust.channel')}
        <select
          id={`${base}-ch`}
          className="rounded border border-line bg-surface px-1 py-0.5 text-xs text-ink"
          value={value.channel}
          onChange={(event) => onChange({ ...value, channel: event.target.value as Channel })}
        >
          {CHANNEL_KEYS.map((channel) => (
            <option key={channel.value} value={channel.value}>
              {t(`adjust.channels.${channel.key}`)}
            </option>
          ))}
        </select>
      </label>
      <button
        type="button"
        disabled={isNeutral(value)}
        onClick={() => onChange(NEUTRAL)}
        className="rounded border border-line px-2 py-0.5 hover:bg-line/30 disabled:opacity-50"
      >
        {t('adjust.reset')}
      </button>
    </div>
  )
}
