/**
 * Display adjustments for the image under the annotations (IMG-5): window /
 * level (brightness and contrast are the same two numbers under friendlier
 * names) and a single-channel view. Only the display changes — the media and
 * the annotations are untouched — and only the image layer, never the shapes.
 *
 * The adjustment is an SVG filter applied as a CSS filter to the image
 * layer's canvas, so the browser does it on the GPU without copying pixels:
 * it stays cheap on large images, where Konva's per-pixel filters are not.
 */

export type Channel = 'rgb' | 'r' | 'g' | 'b' | 'gray'

export interface ImageAdjustment {
  /** Window width in 8-bit display units: 255 is the full range; smaller is more contrast. */
  window: number
  /** Window centre (level): 127.5 is neutral; lower is brighter. */
  level: number
  channel: Channel
}

export const NEUTRAL: ImageAdjustment = { window: 255, level: 127.5, channel: 'rgb' }
export const MIN_WINDOW = 1
export const MAX_WINDOW = 1020

export function isNeutral(adjustment: ImageAdjustment): boolean {
  return (
    adjustment.window === NEUTRAL.window &&
    adjustment.level === NEUTRAL.level &&
    adjustment.channel === NEUTRAL.channel
  )
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value))
}

/** `out = slope · in + intercept` on 0…1 values, mapping the window to the full range. */
export function transfer({ window, level }: ImageAdjustment): { slope: number; intercept: number } {
  const w = clamp(window, MIN_WINDOW, MAX_WINDOW) / 255
  const l = level / 255
  return { slope: 1 / w, intercept: 0.5 - l / w }
}

/** Brightness −100…100 ↔ level; +100 moves the window centre to the bottom of the range. */
export function brightnessOf(adjustment: ImageAdjustment): number {
  return Math.round(((127.5 - adjustment.level) / 127.5) * 100)
}

export function withBrightness(adjustment: ImageAdjustment, brightness: number): ImageAdjustment {
  return { ...adjustment, level: 127.5 - (clamp(brightness, -100, 100) / 100) * 127.5 }
}

/** Contrast −100…100 ↔ window, logarithmic: +50 halves the window, −50 doubles it. */
export function contrastOf(adjustment: ImageAdjustment): number {
  return Math.round(-50 * Math.log2(adjustment.window / 255))
}

export function withContrast(adjustment: ImageAdjustment, contrast: number): ImageAdjustment {
  const window = 255 * 2 ** (-clamp(contrast, -100, 100) / 50)
  return { ...adjustment, window: clamp(window, MIN_WINDOW, MAX_WINDOW) }
}

const LUMA = '0.2126 0.7152 0.0722 0 0'
const ROWS: Record<Channel, string> = {
  rgb: '1 0 0 0 0  0 1 0 0 0  0 0 1 0 0',
  r: '1 0 0 0 0  1 0 0 0 0  1 0 0 0 0',
  g: '0 1 0 0 0  0 1 0 0 0  0 1 0 0 0',
  b: '0 0 1 0 0  0 0 1 0 0  0 0 1 0 0',
  gray: `${LUMA}  ${LUMA}  ${LUMA}`,
}

/** The `feColorMatrix` values showing one channel (or luminance) as grey. */
export function channelMatrix(channel: Channel): string {
  return `${ROWS[channel]}  0 0 0 1 0`
}
