import { describe, expect, it } from 'vitest'
import {
  brightnessOf,
  channelMatrix,
  contrastOf,
  isNeutral,
  NEUTRAL,
  transfer,
  withBrightness,
  withContrast,
} from './adjustment'

/** Apply the transfer to an 8-bit value, as the SVG filter does (clamped). */
function display(value: number, adjustment = NEUTRAL): number {
  const { slope, intercept } = transfer(adjustment)
  return Math.round(Math.min(1, Math.max(0, slope * (value / 255) + intercept)) * 255)
}

describe('image adjustment', () => {
  it('is the identity when neutral', () => {
    expect(transfer(NEUTRAL)).toEqual({ slope: 1, intercept: 0 })
    expect(isNeutral(NEUTRAL)).toBe(true)
    for (const value of [0, 64, 200, 255]) expect(display(value)).toBe(value)
  })

  it('maps the window onto the full display range', () => {
    // A CT-style soft-tissue window: 40 ± 40 of 255.
    const window = { ...NEUTRAL, window: 80, level: 40 }
    expect(display(0, window)).toBe(0)
    expect(display(40, window)).toBe(128)
    expect(display(80, window)).toBe(255)
    expect(display(200, window)).toBe(255)
  })

  it('round-trips brightness and contrast through level and window', () => {
    const brighter = withBrightness(NEUTRAL, 40)
    expect(brightnessOf(brighter)).toBe(40)
    expect(display(100, brighter)).toBeGreaterThan(100)

    const sharper = withContrast(NEUTRAL, 50)
    expect(sharper.window).toBeCloseTo(127.5)
    expect(contrastOf(sharper)).toBe(50)
    expect(withContrast(NEUTRAL, -100).window).toBe(1020)
    expect(withBrightness(NEUTRAL, 500).level).toBe(0)
  })

  it('builds a colour matrix per channel', () => {
    expect(channelMatrix('rgb').split(/\s+/)).toHaveLength(20)
    expect(channelMatrix('g').startsWith('0 1 0 0 0  0 1 0 0 0  0 1 0 0 0')).toBe(true)
    expect(channelMatrix('gray')).toContain('0.2126 0.7152 0.0722')
  })
})
