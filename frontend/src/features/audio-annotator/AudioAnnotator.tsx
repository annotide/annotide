/**
 * Audio annotator (§5): segments on a waveform, with a speaker and a
 * transcript per segment; item-level labels are the classification panel.
 *
 * The file plays in an `<audio>` element straight from its signed URL
 * (ARC-3). The waveform is drawn from the decoded file, except for files
 * over `MAX_WAVEFORM_BYTES`, which play with a plain track: decoding an hour
 * of audio takes gigabytes of memory. Drag across the track to mark a
 * segment of the active class; "Add at playhead" and the segment list do the
 * same from the keyboard. K plays and pauses, Delete removes the selected
 * segment.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { useItemBytes } from '@/api/queries'
import type { AnnotationResult, LabelClass, SegmentShape } from '@/api/types'
import { useHotkeys } from '@/lib/hotkeys'
import { newShapeId } from '@/lib/ids'

import { SegmentList } from '../segments/SegmentList'
import { SegmentTimeline } from '../segments/SegmentTimeline'
import {
  computePeaks,
  formatClock,
  normaliseInterval,
  removeShapeById,
  segmentsOf,
  upsertSegment,
} from '../segments/segments'

/** Larger files play without a drawn waveform. */
export const MAX_WAVEFORM_BYTES = 64 * 1024 * 1024
const PEAK_BUCKETS = 1200
/** Length of a segment added from the keyboard. */
const DEFAULT_SEGMENT_MS = 2000

export interface Waveform {
  peaks: Array<[number, number]>
  durationMs: number
}

export interface AudioAnnotatorProps {
  mediaUrl: string
  /** The item's size, to decide whether to decode a waveform at all. */
  sizeBytes?: number | null
  classes: LabelClass[]
  value: AnnotationResult
  onChange: (next: AnnotationResult) => void
  readOnly?: boolean
  onSelectionChange?: (id: string | null) => void
  /** Tests (jsdom has no Web Audio) hand the waveform in. */
  waveform?: Waveform
}

async function decodeWaveform(bytes: ArrayBuffer): Promise<Waveform> {
  const Context =
    window.AudioContext ??
    (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
  if (!Context) throw new Error('This browser cannot decode audio.')
  const context = new Context()
  try {
    const buffer = await context.decodeAudioData(bytes.slice(0))
    return {
      peaks: computePeaks(buffer.getChannelData(0), PEAK_BUCKETS),
      durationMs: Math.round(buffer.duration * 1000),
    }
  } finally {
    void context.close()
  }
}

function WaveformCanvas({ peaks }: { peaks: Array<[number, number]> }): JSX.Element {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  useEffect(() => {
    const canvas = canvasRef.current
    const context = canvas?.getContext('2d')
    if (!canvas || !context) return
    const { width, height } = canvas
    context.clearRect(0, 0, width, height)
    context.fillStyle = getComputedStyle(canvas).color || '#64748b'
    const mid = height / 2
    peaks.forEach(([low, high], index) => {
      const x = (index / peaks.length) * width
      const top = mid - high * mid
      const bottom = mid - low * mid
      context.fillRect(x, top, Math.max(1, width / peaks.length), Math.max(1, bottom - top))
    })
  }, [peaks])
  return (
    <canvas
      ref={canvasRef}
      width={PEAK_BUCKETS}
      height={120}
      aria-hidden="true"
      className="h-full w-full text-muted"
    />
  )
}

export function AudioAnnotator({
  mediaUrl,
  sizeBytes,
  classes,
  value,
  onChange,
  readOnly = false,
  onSelectionChange,
  waveform: givenWaveform,
}: AudioAnnotatorProps): JSX.Element {
  const { t } = useTranslation('annotator')
  const segmentClasses = useMemo(() => classes.filter((c) => c.tools.includes('segment')), [classes])
  const [chosenClass, setActiveClass] = useState<string | null>(null)
  // The schema can arrive after the first render: fall back to its first
  // segment class rather than remembering an empty choice.
  const activeClass =
    chosenClass && segmentClasses.some((cls) => cls.name === chosenClass)
      ? chosenClass
      : (segmentClasses[0]?.name ?? null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [playheadMs, setPlayheadMs] = useState(0)
  const [playing, setPlaying] = useState(false)
  const [mediaDurationMs, setMediaDurationMs] = useState<number | null>(null)
  const audioRef = useRef<HTMLAudioElement>(null)

  const tooLarge = (sizeBytes ?? 0) > MAX_WAVEFORM_BYTES
  const bytes = useItemBytes(givenWaveform || tooLarge ? null : mediaUrl)
  const [decoded, setDecoded] = useState<Waveform | null>(null)
  const [decodeError, setDecodeError] = useState<string | null>(null)
  useEffect(() => {
    if (!bytes.data) return
    let cancelled = false
    decodeWaveform(bytes.data)
      .then((result) => !cancelled && setDecoded(result))
      .catch((error: unknown) => !cancelled && setDecodeError(String(error)))
    return () => {
      cancelled = true
    }
  }, [bytes.data])

  const waveform = givenWaveform ?? decoded
  const durationMs = waveform?.durationMs ?? mediaDurationMs
  const segments = segmentsOf(value.shapes)

  function select(id: string | null): void {
    setSelectedId(id)
    onSelectionChange?.(id)
  }

  function writeSegment(segment: SegmentShape): void {
    onChange({ ...value, shapes: upsertSegment(value.shapes, segment) })
  }

  function create(a: number, b: number): void {
    if (readOnly || !activeClass || durationMs === null) return
    const interval = normaliseInterval(a, b, 0, durationMs, true)
    if (!interval) return
    const segment: SegmentShape = {
      id: newShapeId(),
      type: 'segment',
      class: activeClass,
      attributes: {},
      confidence: null,
      ...interval,
    }
    writeSegment(segment)
    select(segment.id)
  }

  function remove(id: string): void {
    onChange({ ...value, shapes: removeShapeById(value.shapes, id) })
    if (selectedId === id) select(null)
  }

  function togglePlay(): void {
    const audio = audioRef.current
    if (!audio) return
    if (audio.paused) void audio.play()
    else audio.pause()
  }

  function seek(ms: number): void {
    const audio = audioRef.current
    if (audio) audio.currentTime = ms / 1000
    setPlayheadMs(ms)
  }

  function removeSelected(): void {
    if (selectedId && !readOnly) remove(selectedId)
  }

  useHotkeys({ k: togglePlay, delete: removeSelected, backspace: removeSelected })

  return (
    <div className="flex w-full max-w-5xl flex-col gap-3 self-start">
      <audio
        ref={audioRef}
        src={mediaUrl}
        crossOrigin="anonymous"
        preload="metadata"
        onLoadedMetadata={(event) => {
          const seconds = event.currentTarget.duration
          if (Number.isFinite(seconds)) setMediaDurationMs(Math.round(seconds * 1000))
        }}
        onTimeUpdate={(event) => setPlayheadMs(Math.round(event.currentTarget.currentTime * 1000))}
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        className="hidden"
      />

      <div className="flex flex-wrap items-center gap-3 text-sm text-ink">
        <button
          type="button"
          onClick={togglePlay}
          className="rounded-md border border-line px-3 py-1 hover:bg-line/30"
        >
          {playing ? t('segments.pause') : t('segments.play')}
        </button>
        <span className="font-mono text-xs text-muted" aria-live="off">
          {formatClock(playheadMs)}
          {durationMs !== null && ` / ${formatClock(durationMs)}`}
        </span>
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
            <button
              type="button"
              disabled={durationMs === null}
              onClick={() => create(playheadMs, playheadMs + DEFAULT_SEGMENT_MS)}
              className="rounded-md border border-line px-3 py-1 hover:bg-line/30 disabled:opacity-50"
            >
              {t('segments.addAtPlayhead')}
            </button>
          </>
        )}
      </div>

      {segmentClasses.length === 0 && (
        <p role="alert" className="text-sm text-danger">
          {t('segments.noClasses')}
        </p>
      )}
      {(bytes.isError || decodeError) && (
        <p className="text-xs text-muted">{t('segments.waveformUnavailable')}</p>
      )}
      {tooLarge && <p className="text-xs text-muted">{t('segments.waveformTooLarge')}</p>}

      {durationMs === null ? (
        <p className="text-sm text-muted">{t('segments.loadingAudio')}</p>
      ) : (
        <SegmentTimeline
          min={0}
          max={durationMs}
          segments={segments}
          classes={classes}
          selectedId={selectedId}
          readOnly={readOnly}
          playhead={playheadMs}
          onCreate={create}
          onSelect={(id) => select(id)}
          onSeek={seek}
          label={t('segments.timelineLabel')}
        >
          {waveform && <WaveformCanvas peaks={waveform.peaks} />}
        </SegmentTimeline>
      )}

      <SegmentList
        segments={segments}
        classes={segmentClasses}
        kind="audio"
        selectedId={selectedId}
        readOnly={readOnly}
        onChange={(segment) => {
          if (segment.end > segment.start) writeSegment(segment)
        }}
        onRemove={remove}
        onSelect={(id) => select(id)}
      />
    </div>
  )
}
