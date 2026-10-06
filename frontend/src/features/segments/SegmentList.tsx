/**
 * Every segment as an editable row (§5): class, start, end, speaker and
 * transcript for audio, covered channels and a note for time series. Start
 * and end apply on blur or Enter; a start past the end moves the whole
 * segment, and an empty or backwards interval is reverted. The whole job can be done here with a keyboard (UX-7); the
 * timeline above only makes the common case faster.
 */

import { useLayoutEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { LabelClass, SegmentShape } from '@/api/types'

import { formatClock } from './segments'

export interface SegmentListProps {
  segments: SegmentShape[]
  classes: LabelClass[]
  /** `audio`: positions are milliseconds, shown and edited in seconds. */
  kind: 'audio' | 'timeseries'
  /** Time-series channel names, for the per-segment channel choice. */
  channels?: string[]
  selectedId: string | null
  readOnly?: boolean
  onChange: (segment: SegmentShape) => void
  onRemove: (id: string) => void
  onSelect: (id: string) => void
}

const INPUT =
  'rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink disabled:opacity-60'

/**
 * A number field that applies on blur or Enter. A half-typed value may be
 * briefly invalid (empty, or an end before the start), so it is not pushed
 * on every keystroke; a rejected value reverts. A change from elsewhere (a
 * drag, or the other bound moving the segment) shows up unless the person
 * has typed in the field, and the field is never remounted, so focus stays.
 *
 * Replacing the text of a focused field drops its selection, so after
 * Tab (which selects all) from a start that moved the segment, typing would
 * append to the new end ("139.9" + "135"). A focused field that is not being
 * edited therefore selects its text again whenever it changes.
 */
function CommittedNumber({
  label,
  value,
  step,
  onCommit,
}: {
  label: string
  value: number
  step: number | 'any'
  onCommit: (value: number) => boolean
}): JSX.Element {
  // What the person has typed and not yet applied; null shows `value`.
  const [draft, setDraft] = useState<string | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const editing = draft !== null
  useLayoutEffect(() => {
    if (!editing && input.current !== null && input.current === document.activeElement) {
      input.current.select()
    }
  }, [value, editing])

  function commit(): void {
    if (draft === null) return
    setDraft(null)
    const parsed = Number(draft)
    // A rejected value needs nothing more: with no draft the field shows `value`.
    if (draft.trim() !== '' && Number.isFinite(parsed)) onCommit(parsed)
  }

  return (
    <input
      ref={input}
      type="number"
      step={step}
      min={0}
      aria-label={label}
      value={draft ?? String(value)}
      onChange={(event) => setDraft(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === 'Enter') commit()
      }}
      onBlur={commit}
      className={`${INPUT} w-28`}
    />
  )
}

export function SegmentList({
  segments,
  classes,
  kind,
  channels = [],
  selectedId,
  readOnly = false,
  onChange,
  onRemove,
  onSelect,
}: SegmentListProps): JSX.Element {
  const { t } = useTranslation('annotator')
  const audio = kind === 'audio'
  const toInput = (value: number): number => (audio ? value / 1000 : value)
  const fromInput = (value: number): number => (audio ? Math.round(value * 1000) : value)

  if (segments.length === 0) {
    return <p className="text-sm text-muted">{t('segments.empty')}</p>
  }

  return (
    <ol className="space-y-2" aria-label={t('segments.listLabel')}>
      {segments.map((segment, index) => {
        const selected = segment.id === selectedId
        const covered = segment.channels ?? channels
        const label = t('segments.rowLabel', { index: index + 1 })
        return (
          <li
            key={segment.id}
            className={`rounded-md border p-2 ${selected ? 'border-accent' : 'border-line'}`}
            onFocusCapture={() => onSelect(segment.id)}
          >
            <fieldset className="flex flex-wrap items-end gap-2" disabled={readOnly}>
              <legend className="mb-1 text-xs font-medium text-muted">
                {label}
                {audio && ` · ${formatClock(segment.start)}–${formatClock(segment.end)}`}
              </legend>
              <label className="flex flex-col text-xs text-muted">
                {t('segments.class')}
                <select
                  aria-label={`${label}: ${t('segments.class')}`}
                  value={segment.class}
                  onChange={(event) => onChange({ ...segment, class: event.target.value })}
                  className={INPUT}
                >
                  {classes.map((cls) => (
                    <option key={cls.name} value={cls.name}>
                      {cls.display_name || cls.name}
                    </option>
                  ))}
                </select>
              </label>
              {(['start', 'end'] as const).map((bound) => (
                <label key={bound} className="flex flex-col text-xs text-muted">
                  {t(audio ? `segments.${bound}Seconds` : `segments.${bound}`)}
                  <CommittedNumber
                    label={`${label}: ${t(`segments.${bound}`)}`}
                    step={audio ? 0.001 : 'any'}
                    value={toInput(segment[bound])}
                    onCommit={(value) => {
                      const next = { ...segment, [bound]: fromInput(value) }
                      // A start moved past the end takes the whole segment
                      // along, keeping its length.
                      if (bound === 'start' && next.start >= segment.end) {
                        next.end = next.start + (segment.end - segment.start)
                      }
                      if (next.end <= next.start) return false
                      onChange(next)
                      return true
                    }}
                  />
                </label>
              ))}
              {audio && (
                <label className="flex flex-col text-xs text-muted">
                  {t('segments.speaker')}
                  <input
                    aria-label={`${label}: ${t('segments.speaker')}`}
                    value={segment.speaker ?? ''}
                    onChange={(event) =>
                      onChange({ ...segment, speaker: event.target.value || null })
                    }
                    className={`${INPUT} w-32`}
                  />
                </label>
              )}
              <button
                type="button"
                onClick={() => onRemove(segment.id)}
                className="ml-auto rounded-md border border-line px-2 py-1 text-sm text-danger hover:bg-line/30"
              >
                {t('segments.remove', { label })}
              </button>
            </fieldset>
            {!audio && channels.length > 1 && (
              <fieldset className="mt-2 flex flex-wrap gap-3 text-sm text-ink" disabled={readOnly}>
                <legend className="text-xs text-muted">{t('segments.channels')}</legend>
                {channels.map((name) => (
                  <label key={name} className="flex items-center gap-1">
                    <input
                      type="checkbox"
                      checked={covered.includes(name)}
                      onChange={(event) => {
                        const next = event.target.checked
                          ? channels.filter((c) => c === name || covered.includes(c))
                          : covered.filter((c) => c !== name)
                        if (next.length === 0) return
                        onChange({
                          ...segment,
                          channels: next.length === channels.length ? null : next,
                        })
                      }}
                    />
                    {name}
                  </label>
                ))}
              </fieldset>
            )}
            <label className="mt-2 flex flex-col text-xs text-muted">
              {t(audio ? 'segments.transcript' : 'segments.note')}
              <textarea
                rows={audio ? 2 : 1}
                aria-label={`${label}: ${t(audio ? 'segments.transcript' : 'segments.note')}`}
                value={segment.text ?? ''}
                disabled={readOnly}
                onChange={(event) => onChange({ ...segment, text: event.target.value || null })}
                className={`${INPUT} w-full`}
              />
            </label>
          </li>
        )
      })}
    </ol>
  )
}
