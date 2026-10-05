/** Tool picker and viewport controls. Every control has a hotkey (UX-1). */

import { useTranslation } from 'react-i18next'
import { MAX_SEGMENTS, MIN_SEGMENTS } from './superpixels'
import { TOOLS } from './toolHotkeys'
import type { Tool } from './types'

export interface ToolbarProps {
  tool: Tool
  onToolChange: (tool: Tool) => void
  onZoomIn: () => void
  onZoomOut: () => void
  onFit: () => void
  onUndo: () => void
  onRedo: () => void
  canUndo: boolean
  canRedo: boolean
  scale: number
  readOnly: boolean
  /** Offers the smart-polygon tool (ML-7); off when no segment model exists. */
  smartEnabled?: boolean
  /** Image display adjustments (IMG-5): whether the panel is open, and a toggle. */
  adjustOpen?: boolean
  onToggleAdjust?: () => void
  /** An adjustment is in effect, so the button shows it even when closed. */
  adjusted?: boolean
  /** Offers the keypoints tool; on when a class has a skeleton. */
  keypointsEnabled?: boolean
  /** Offers the mask brush and eraser; off for tiled or very large images. */
  brushEnabled?: boolean
  /** Brush diameter in screen pixels, and a setter ([ / ] also change it). */
  brushSize?: number
  onBrushSizeChange?: (size: number) => void
  /** Offers the superpixel tool; needs the brush's conditions and a loaded
   * (not tiled) image. */
  superpixelsEnabled?: boolean
  /** About how many superpixels the image is split into, and a setter. */
  superpixelSegments?: number
  onSuperpixelSegmentsChange?: (segments: number) => void
}

export const MIN_BRUSH_SIZE = 2
export const MAX_BRUSH_SIZE = 200

type ToolTitleKey =
  | 'toolbar.titleSmart'
  | 'toolbar.titleBrush'
  | 'toolbar.titleEraser'
  | 'toolbar.titleSuperpixel'
  | 'toolbar.titleKeypoints'

const TOOL_TITLE_KEY: Partial<Record<Tool, ToolTitleKey>> = {
  smart: 'toolbar.titleSmart',
  brush: 'toolbar.titleBrush',
  eraser: 'toolbar.titleEraser',
  superpixel: 'toolbar.titleSuperpixel',
  keypoints: 'toolbar.titleKeypoints',
}

const buttonClass =
  'rounded px-2 py-1 text-xs font-medium transition-colors focus-visible:outline ' +
  'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-accent ' +
  'disabled:cursor-not-allowed disabled:opacity-40'

export function Toolbar({
  tool,
  onToolChange,
  onZoomIn,
  onZoomOut,
  onFit,
  onUndo,
  onRedo,
  canUndo,
  canRedo,
  scale,
  readOnly,
  smartEnabled = false,
  adjustOpen = false,
  onToggleAdjust,
  adjusted = false,
  keypointsEnabled = false,
  brushEnabled = false,
  brushSize,
  onBrushSizeChange,
  superpixelsEnabled = false,
  superpixelSegments,
  onSuperpixelSegmentsChange,
}: ToolbarProps) {
  const { t } = useTranslation('annotator')
  return (
    <div
      role="toolbar"
      aria-label={t('toolbar.ariaLabel')}
      className="flex flex-wrap items-center gap-1 border-b border-line bg-surface px-2 py-1.5"
    >
      {TOOLS.filter(
        ({ tool: value }) =>
          (value !== 'smart' || smartEnabled) &&
          (value !== 'keypoints' || keypointsEnabled) &&
          ((value !== 'brush' && value !== 'eraser') || brushEnabled) &&
          (value !== 'superpixel' || superpixelsEnabled),
      ).map(({ tool: value, hotkey }) => {
        const active = tool === value
        const disabled = readOnly && value !== 'select' && value !== 'measure'
        const label = t(`tools.${value}`)
        return (
          <button
            key={value}
            type="button"
            className={`${buttonClass} ${
              active ? 'bg-accent-fill text-white' : 'text-ink hover:bg-line/40'
            }`}
            aria-pressed={active}
            disabled={disabled}
            title={t(TOOL_TITLE_KEY[value] ?? 'toolbar.titleDefault', { label, hotkey })}
            onClick={() => onToolChange(value)}
          >
            {label}
            <span className="ml-1 opacity-60">{hotkey}</span>
          </button>
        )
      })}

      {(tool === 'brush' || tool === 'eraser') && onBrushSizeChange && brushSize !== undefined && (
        <label
          className="ml-1 flex items-center gap-1 text-xs text-muted"
          title={t('toolbar.brushSizeTitle')}
        >
          {t('toolbar.size')}
          <input
            type="range"
            min={MIN_BRUSH_SIZE}
            max={MAX_BRUSH_SIZE}
            value={brushSize}
            onChange={(event) => onBrushSizeChange(Number(event.target.value))}
            className="w-24"
            aria-label={t('toolbar.brushSizeLabel')}
          />
          <span className="w-8 tabular-nums">{brushSize}</span>
        </label>
      )}

      {tool === 'superpixel' && onSuperpixelSegmentsChange && superpixelSegments !== undefined && (
        <label
          className="ml-1 flex items-center gap-1 text-xs text-muted"
          title={t('toolbar.superpixelDetailTitle')}
        >
          {t('toolbar.detail')}
          <input
            type="range"
            min={MIN_SEGMENTS}
            max={MAX_SEGMENTS}
            step={100}
            value={superpixelSegments}
            onChange={(event) => onSuperpixelSegmentsChange(Number(event.target.value))}
            className="w-24"
            aria-label={t('toolbar.superpixelDetailLabel')}
          />
          <span className="w-10 tabular-nums">{superpixelSegments}</span>
        </label>
      )}

      <span className="mx-1 h-4 w-px bg-line" aria-hidden="true" />

      <button
        type="button"
        className={`${buttonClass} text-ink hover:bg-line/40`}
        onClick={onZoomOut}
        title={t('toolbar.zoomOut')}
      >
        &minus;
      </button>
      <span className="min-w-14 text-center text-xs tabular-nums text-muted" aria-live="polite">
        {Math.round(scale * 100)}%
      </span>
      <button
        type="button"
        className={`${buttonClass} text-ink hover:bg-line/40`}
        onClick={onZoomIn}
        title={t('toolbar.zoomIn')}
      >
        +
      </button>
      <button
        type="button"
        className={`${buttonClass} text-ink hover:bg-line/40`}
        onClick={onFit}
        title={t('toolbar.fitTitle')}
      >
        {t('toolbar.fit')}
      </button>
      {onToggleAdjust && (
        <button
          type="button"
          className={`${buttonClass} ${
            adjustOpen || adjusted ? 'bg-accent/15 text-accent' : 'text-ink hover:bg-line/40'
          }`}
          onClick={onToggleAdjust}
          aria-pressed={adjustOpen}
          title={t('toolbar.imageTitle')}
        >
          {t('toolbar.image')}
        </button>
      )}

      <span className="mx-1 h-4 w-px bg-line" aria-hidden="true" />

      <button
        type="button"
        className={`${buttonClass} text-ink hover:bg-line/40`}
        onClick={onUndo}
        disabled={!canUndo || readOnly}
        title={t('toolbar.undoTitle')}
      >
        {t('toolbar.undo')}
      </button>
      <button
        type="button"
        className={`${buttonClass} text-ink hover:bg-line/40`}
        onClick={onRedo}
        disabled={!canRedo || readOnly}
        title={t('toolbar.redoTitle')}
      >
        {t('toolbar.redo')}
      </button>

      {readOnly && (
        <span className="ml-2 rounded bg-line/40 px-2 py-0.5 text-xs text-muted">
          {t('toolbar.readOnly')}
        </span>
      )}
    </div>
  )
}
