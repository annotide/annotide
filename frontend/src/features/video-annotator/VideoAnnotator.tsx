/**
 * Video annotator (TOOL): bounding-box tracks with keyframes and linear
 * interpolation between them (docs/CONTRACTS.md, "Video items"). A plain
 * <video> element drives playback; an absolutely positioned SVG overlay, in
 * an image-pixel viewBox, draws and edits boxes on top of it.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { AnnotationResult, BBox, LabelClass, Shape } from '@/api/types'
import { useHotkeys } from '@/lib/hotkeys'

import { bboxFromCorners, clientToImagePoint, translateBBox } from './pointer'
import { boxAt, frameOf, groupTracks, markOutside, removeKeyframe, removeTrack, setKeyframe } from './tracks'
import type { Track } from './tracks'

export interface VideoAnnotatorProps {
  videoUrl: string
  width: number
  height: number
  fps: number
  classes: LabelClass[]
  value: AnnotationResult
  onChange: (next: AnnotationResult) => void
  readOnly?: boolean
  onSelectionChange?: (id: string | null) => void
}

type DragState =
  | { kind: 'draw'; start: [number, number]; current: [number, number] }
  | { kind: 'move'; trackId: string; origBBox: BBox; start: [number, number]; current: [number, number] }

function colorFor(classes: LabelClass[], className: string): string {
  return classes.find((c) => c.name === className)?.color ?? '#94a3b8'
}

export function VideoAnnotator({
  videoUrl,
  width,
  height,
  fps,
  classes,
  value,
  onChange,
  readOnly = false,
  onSelectionChange,
}: VideoAnnotatorProps): JSX.Element {
  const { t } = useTranslation('annotator')
  const videoRef = useRef<HTMLVideoElement>(null)
  const svgRef = useRef<SVGSVGElement>(null)

  const [frame, setFrame] = useState(0)
  const [isPlaying, setIsPlaying] = useState(false)
  const [selectedTrackId, setSelectedTrackIdState] = useState<string | null>(null)
  const [activeClassName, setActiveClassName] = useState<string | null>(null)
  const [drag, setDrag] = useState<DragState | null>(null)

  const boxClasses = useMemo(() => classes.filter((c) => c.tools.includes('bbox')), [classes])
  const currentClass = activeClassName ?? boxClasses[0]?.name ?? null

  const select = useCallback(
    (id: string | null) => {
      setSelectedTrackIdState(id)
      onSelectionChange?.(id)
    },
    [onSelectionChange],
  )

  const tracks = useMemo(() => groupTracks(value.shapes), [value.shapes])
  const selectedTrack = tracks.find((t) => t.trackId === selectedTrackId) ?? null

  const setShapes = useCallback(
    (shapes: Shape[]) => onChange({ ...value, media_type: 'video', shapes }),
    [onChange, value],
  )

  const onTimeUpdate = useCallback(() => {
    const video = videoRef.current
    if (!video) return
    setFrame(frameOf(video.currentTime, fps))
  }, [fps])

  const seekToFrame = useCallback(
    (target: number) => {
      const next = Math.max(0, target)
      const video = videoRef.current
      if (video) video.currentTime = next / fps
      setFrame(next)
    },
    [fps],
  )

  const togglePlay = useCallback(() => {
    const video = videoRef.current
    if (!video) return
    if (video.paused) void video.play()
    else video.pause()
  }, [])

  const imagePoint = useCallback(
    (clientX: number, clientY: number): [number, number] => {
      const rect = svgRef.current?.getBoundingClientRect() ?? { left: 0, top: 0, width, height }
      return clientToImagePoint(rect, width, height, clientX, clientY)
    },
    [width, height],
  )

  const handleBackgroundMouseDown = useCallback(
    (event: React.MouseEvent<SVGSVGElement>) => {
      if (readOnly) return
      if ((event.target as HTMLElement).dataset?.role === 'box') return
      const point = imagePoint(event.clientX, event.clientY)
      select(null)
      setDrag({ kind: 'draw', start: point, current: point })
    },
    [imagePoint, readOnly, select],
  )

  const handleBoxMouseDown = useCallback(
    (event: React.MouseEvent<SVGRectElement>, track: Track, bbox: BBox) => {
      if (readOnly) return
      event.stopPropagation()
      select(track.trackId)
      const point = imagePoint(event.clientX, event.clientY)
      setDrag({ kind: 'move', trackId: track.trackId, origBBox: bbox, start: point, current: point })
    },
    [imagePoint, readOnly, select],
  )

  useEffect(() => {
    if (!drag) return undefined

    function onMove(event: MouseEvent): void {
      const point = imagePoint(event.clientX, event.clientY)
      setDrag((prev) => (prev ? { ...prev, current: point } : prev))
    }

    function onUp(): void {
      setDrag((prev) => {
        if (!prev) return null
        if (prev.kind === 'draw') {
          const bbox = bboxFromCorners(prev.start, prev.current)
          if (Math.abs(bbox[2] - bbox[0]) >= 2 && Math.abs(bbox[3] - bbox[1]) >= 2 && currentClass) {
            const trackId = crypto.randomUUID()
            setShapes(setKeyframe(value.shapes, trackId, frame, bbox, currentClass))
            select(trackId)
          }
        } else {
          const track = tracks.find((t) => t.trackId === prev.trackId)
          if (track) {
            const moved = translateBBox(prev.origBBox, prev.start, prev.current)
            setShapes(setKeyframe(value.shapes, prev.trackId, frame, moved, track.class))
          }
        }
        return null
      })
    }

    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [drag !== null])

  const handleOutside = useCallback(() => {
    if (readOnly || !selectedTrackId) return
    setShapes(markOutside(value.shapes, selectedTrackId, frame))
  }, [frame, readOnly, selectedTrackId, setShapes, value.shapes])

  const handleDeleteKeyframe = useCallback(() => {
    if (readOnly || !selectedTrackId) return
    setShapes(removeKeyframe(value.shapes, selectedTrackId, frame))
  }, [frame, readOnly, selectedTrackId, setShapes, value.shapes])

  const handleDeleteTrack = useCallback(() => {
    if (readOnly || !selectedTrackId) return
    setShapes(removeTrack(value.shapes, selectedTrackId))
    select(null)
  }, [readOnly, select, selectedTrackId, setShapes, value.shapes])

  useHotkeys(
    {
      arrowleft: () => seekToFrame(frame - 1),
      arrowright: () => seekToFrame(frame + 1),
      o: handleOutside,
      delete: handleDeleteKeyframe,
      backspace: handleDeleteKeyframe,
    },
    !readOnly,
  )

  const duration = videoRef.current?.duration
  const totalFrames = Number.isFinite(duration) && (duration ?? 0) > 0
    ? Math.max(1, Math.floor((duration ?? 0) * fps))
    : Math.max(frame + 1, ...(selectedTrack?.keyframes.map((k) => (k.frame ?? 0) + 1) ?? [1]))

  return (
    <div className="flex flex-col gap-2">
      <div
        className="relative"
        style={{ width: '100%', maxWidth: width, aspectRatio: `${width} / ${height}` }}
      >
        <video
          ref={videoRef}
          src={videoUrl}
          className="absolute inset-0 h-full w-full"
          onTimeUpdate={onTimeUpdate}
          onPlay={() => setIsPlaying(true)}
          onPause={() => setIsPlaying(false)}
          data-testid="video-element"
        />
        <svg
          ref={svgRef}
          viewBox={`0 0 ${width} ${height}`}
          className="absolute inset-0 h-full w-full"
          data-testid="video-overlay"
          onMouseDown={handleBackgroundMouseDown}
        >
          {tracks.map((track) => {
            let box = boxAt(track, frame)
            let bboxForMove = box?.bbox
            if (drag?.kind === 'move' && drag.trackId === track.trackId) {
              bboxForMove = translateBBox(drag.origBBox, drag.start, drag.current)
              box = { bbox: bboxForMove, keyframe: box?.keyframe ?? true }
            }
            if (!box) return null
            const [x1, y1, x2, y2] = box.bbox
            return (
              <rect
                key={track.trackId}
                data-role="box"
                data-testid={`track-box-${track.trackId}`}
                x={Math.min(x1, x2)}
                y={Math.min(y1, y2)}
                width={Math.abs(x2 - x1)}
                height={Math.abs(y2 - y1)}
                fill="none"
                strokeWidth={track.trackId === selectedTrackId ? 3 : 2}
                stroke={colorFor(classes, track.class)}
                strokeDasharray={box.keyframe ? undefined : '6 3'}
                onMouseDown={(event) => handleBoxMouseDown(event, track, box!.bbox)}
              />
            )
          })}
          {drag?.kind === 'draw' && (
            <rect
              data-testid="draw-preview"
              x={Math.min(drag.start[0], drag.current[0])}
              y={Math.min(drag.start[1], drag.current[1])}
              width={Math.abs(drag.current[0] - drag.start[0])}
              height={Math.abs(drag.current[1] - drag.start[1])}
              fill="none"
              strokeWidth={2}
              stroke={currentClass ? colorFor(classes, currentClass) : '#94a3b8'}
            />
          )}
        </svg>
      </div>

      <div className="flex items-center gap-2 text-sm text-ink">
        <button
          type="button"
          className="rounded border border-line px-2 py-1"
          onClick={() => seekToFrame(frame - 1)}
        >
          ◀
        </button>
        <button type="button" className="rounded border border-line px-2 py-1" onClick={togglePlay}>
          {isPlaying ? t('video.pause') : t('video.play')}
        </button>
        <button
          type="button"
          className="rounded border border-line px-2 py-1"
          onClick={() => seekToFrame(frame + 1)}
        >
          ▶
        </button>
        <span data-testid="frame-counter" className="text-muted">
          {t('video.frame', { frame })}
        </span>

        {!readOnly && boxClasses.length > 0 && (
          <label className="ml-4 flex items-center gap-1">
            <span className="text-muted">{t('video.class')}</span>
            <select
              className="rounded border border-line bg-surface px-1 py-0.5 text-ink"
              value={currentClass ?? ''}
              onChange={(event) => setActiveClassName(event.target.value)}
            >
              {boxClasses.map((c) => (
                <option key={c.name} value={c.name}>
                  {c.display_name}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>

      {selectedTrack && (
        <div className="flex flex-col gap-2 rounded border border-line p-2 text-sm text-ink">
          <div className="flex items-center gap-2">
            <span className="text-muted">{t('video.track')}</span>
            <span>{selectedTrack.class}</span>
            {!readOnly && (
              <div className="ml-auto flex gap-2">
                <button
                  type="button"
                  className="rounded border border-line px-2 py-1"
                  onClick={handleOutside}
                >
                  {t('video.outsideFromHere')}
                </button>
                <button
                  type="button"
                  className="rounded border border-line px-2 py-1"
                  onClick={handleDeleteKeyframe}
                >
                  {t('video.deleteKeyframe')}
                </button>
                <button
                  type="button"
                  className="rounded border border-line px-2 py-1"
                  onClick={handleDeleteTrack}
                >
                  {t('video.deleteTrack')}
                </button>
              </div>
            )}
          </div>
          <div
            className="relative h-6 rounded border border-line bg-surface"
            data-testid="track-timeline"
          >
            {selectedTrack.keyframes.map((kf) => {
              const kfFrame = kf.frame ?? 0
              const left = totalFrames > 0 ? (kfFrame / totalFrames) * 100 : 0
              return (
                <button
                  key={kf.id}
                  type="button"
                  aria-label={t('video.seekToFrame', { frame: kfFrame })}
                  title={
                    kf.outside
                      ? t('video.frameTitleOutside', { frame: kfFrame })
                      : t('video.frameTitle', { frame: kfFrame })
                  }
                  className={`absolute top-0 h-6 w-1.5 -translate-x-1/2 ${
                    kf.outside ? 'bg-red-400' : 'bg-accent'
                  }`}
                  style={{ left: `${left}%` }}
                  onClick={() => seekToFrame(kfFrame)}
                />
              )
            })}
            <div
              className="absolute top-0 h-6 w-px bg-ink"
              style={{ left: `${totalFrames > 0 ? (frame / totalFrames) * 100 : 0}%` }}
            />
          </div>
        </div>
      )}
    </div>
  )
}
