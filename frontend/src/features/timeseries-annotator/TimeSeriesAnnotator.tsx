/**
 * Time-series annotator (§5): intervals over several channels of a CSV.
 *
 * Each channel is a lane of its own (scaled to its own min and max), so a
 * small signal is not flattened by a large one. Drag across the plot to mark
 * an interval of the active class; it covers every channel or only the
 * channels shown, as chosen. The segment list edits everything, keyboard
 * included. Delete removes the selected segment.
 */

import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { AnnotationResult, LabelClass, SegmentShape } from '@/api/types'
import { useHotkeys } from '@/lib/hotkeys'
import { newShapeId } from '@/lib/ids'

import { SegmentList } from '../segments/SegmentList'
import { SegmentTimeline } from '../segments/SegmentTimeline'
import { normaliseInterval, removeShapeById, segmentsOf, upsertSegment } from '../segments/segments'
import type { Series } from '../segments/series'
import { downsample } from '../segments/series'

const PLOT_BUCKETS = 600
const LANE_HEIGHT = 56
const LANE_COLOURS = ['#2563eb', '#16a34a', '#d97706', '#db2777', '#7c3aed', '#0891b2']

export interface TimeSeriesAnnotatorProps {
  series: Series
  classes: LabelClass[]
  value: AnnotationResult
  onChange: (next: AnnotationResult) => void
  readOnly?: boolean
  onSelectionChange?: (id: string | null) => void
}

function formatPosition(series: Series, position: number): string {
  return series.timestamps ? new Date(position).toISOString() : String(Number(position.toFixed(6)))
}

function Lanes({ series, shown }: { series: Series; shown: string[] }): JSX.Element {
  const min = series.time[0] ?? 0
  const max = series.time[series.time.length - 1] ?? 1
  const span = max - min || 1
  const lanes = series.channels.filter((channel) => shown.includes(channel.name))
  const height = Math.max(1, lanes.length) * LANE_HEIGHT
  return (
    <svg
      viewBox={`0 0 1000 ${height}`}
      preserveAspectRatio="none"
      className="h-full w-full"
      aria-hidden="true"
    >
      {lanes.map((channel, lane) => {
        const points = downsample(series.time, channel.values, PLOT_BUCKETS)
        const values = points.map(([, v]) => v)
        const low = Math.min(...values)
        const high = Math.max(...values)
        const range = high - low || 1
        const top = lane * LANE_HEIGHT + 4
        const usable = LANE_HEIGHT - 8
        const path = points
          .map(([t, v], i) => {
            const x = ((t - min) / span) * 1000
            const y = top + usable - ((v - low) / range) * usable
            return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
          })
          .join(' ')
        const colour = LANE_COLOURS[series.channels.indexOf(channel) % LANE_COLOURS.length]
        return (
          <g key={channel.name}>
            <path d={path} fill="none" stroke={colour} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
            <text x={4} y={top + 10} fontSize={10} fill={colour}>
              {channel.name}
            </text>
          </g>
        )
      })}
    </svg>
  )
}

export function TimeSeriesAnnotator({
  series,
  classes,
  value,
  onChange,
  readOnly = false,
  onSelectionChange,
}: TimeSeriesAnnotatorProps): JSX.Element {
  const { t } = useTranslation('annotator')
  const segmentClasses = useMemo(() => classes.filter((c) => c.tools.includes('segment')), [classes])
  const names = useMemo(() => series.channels.map((channel) => channel.name), [series])
  const [chosenClass, setActiveClass] = useState<string | null>(null)
  // The schema can arrive after the first render: fall back to its first
  // segment class rather than remembering an empty choice.
  const activeClass =
    chosenClass && segmentClasses.some((cls) => cls.name === chosenClass)
      ? chosenClass
      : (segmentClasses[0]?.name ?? null)
  const [shown, setShown] = useState<string[]>(names)
  const [coverShown, setCoverShown] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const min = series.time[0] ?? 0
  const max = series.time[series.time.length - 1] ?? min + 1
  const segments = segmentsOf(value.shapes)

  function select(id: string | null): void {
    setSelectedId(id)
    onSelectionChange?.(id)
  }

  function write(segment: SegmentShape): void {
    onChange({ ...value, shapes: upsertSegment(value.shapes, segment) })
  }

  function create(a: number, b: number): void {
    if (readOnly || !activeClass) return
    const interval = normaliseInterval(a, b, min, max, false)
    if (!interval) return
    const covers = coverShown && shown.length < names.length ? shown : null
    const segment: SegmentShape = {
      id: newShapeId(),
      type: 'segment',
      class: activeClass,
      attributes: {},
      confidence: null,
      ...interval,
      channels: covers,
    }
    write(segment)
    select(segment.id)
  }

  function remove(id: string): void {
    onChange({ ...value, shapes: removeShapeById(value.shapes, id) })
    if (selectedId === id) select(null)
  }

  function removeSelected(): void {
    if (selectedId && !readOnly) remove(selectedId)
  }

  useHotkeys({ delete: removeSelected, backspace: removeSelected })

  const lanes = Math.max(1, shown.length)

  return (
    <div className="flex w-full max-w-5xl flex-col gap-3 self-start">
      <div className="flex flex-wrap items-center gap-4 text-sm text-ink">
        <fieldset className="flex flex-wrap items-center gap-3">
          <legend className="sr-only">{t('segments.showChannels')}</legend>
          <span className="text-xs text-muted">{t('segments.showChannels')}</span>
          {names.map((name) => (
            <label key={name} className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={shown.includes(name)}
                onChange={(event) =>
                  setShown((prev) => {
                    const next = event.target.checked
                      ? names.filter((n) => n === name || prev.includes(n))
                      : prev.filter((n) => n !== name)
                    return next.length > 0 ? next : prev
                  })
                }
              />
              {name}
            </label>
          ))}
        </fieldset>
        {!readOnly && segmentClasses.length > 0 && (
          <>
            <label className="flex items-center gap-2 text-xs text-muted">
              {t('segments.activeClass')}
              <select
                value={activeClass ?? ''}
                onChange={(event) => setActiveClass(event.target.value)}
                className="rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink"
              >
                {segmentClasses.map((cls) => (
                  <option key={cls.name} value={cls.name}>
                    {cls.display_name || cls.name}
                  </option>
                ))}
              </select>
            </label>
            {names.length > 1 && (
              <label className="flex items-center gap-2 text-xs text-muted">
                {t('segments.newCovers')}
                <select
                  value={coverShown ? 'shown' : 'all'}
                  onChange={(event) => setCoverShown(event.target.value === 'shown')}
                  className="rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink"
                >
                  <option value="all">{t('segments.allChannels')}</option>
                  <option value="shown">{t('segments.shownChannels')}</option>
                </select>
              </label>
            )}
            <button
              type="button"
              onClick={() => create(min, min + (max - min) / 10)}
              className="rounded-md border border-line px-3 py-1 hover:bg-line/30"
            >
              {t('segments.addSelection')}
            </button>
          </>
        )}
      </div>

      {segmentClasses.length === 0 && (
        <p role="alert" className="text-sm text-danger">
          {t('segments.noClasses')}
        </p>
      )}

      <p className="text-xs text-muted">
        {series.timeLabel}:{' '}
        {t('segments.range', { from: formatPosition(series, min), to: formatPosition(series, max) })}
      </p>

      <SegmentTimeline
        min={min}
        max={max}
        segments={segments}
        classes={classes}
        selectedId={selectedId}
        readOnly={readOnly}
        onCreate={create}
        onSelect={(id) => select(id)}
        label={t('segments.timelineLabel')}
        height={lanes * LANE_HEIGHT}
      >
        <Lanes series={series} shown={shown} />
      </SegmentTimeline>

      <SegmentList
        segments={segments}
        classes={segmentClasses}
        kind="timeseries"
        channels={names}
        selectedId={selectedId}
        readOnly={readOnly}
        onChange={(segment) => {
          if (segment.end > segment.start) write(segment)
        }}
        onRemove={remove}
        onSelect={(id) => select(id)}
      />
    </div>
  )
}
