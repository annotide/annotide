import type { Tool } from './types'

// Labels are resolved at render time from the `tools` namespace (below) so a
// language switch applies; this array only carries the stable tool/hotkey
// pairing.
export const TOOLS: ReadonlyArray<{ tool: Tool; hotkey: string }> = [
  { tool: 'select', hotkey: 'V' },
  { tool: 'pan', hotkey: 'H' },
  { tool: 'bbox', hotkey: 'B' },
  { tool: 'rbox', hotkey: 'R' },
  { tool: 'polygon', hotkey: 'G' },
  { tool: 'polyline', hotkey: 'L' },
  { tool: 'point', hotkey: 'P' },
  { tool: 'smart', hotkey: 'S' },
  { tool: 'keypoints', hotkey: 'J' },
  { tool: 'brush', hotkey: 'K' },
  { tool: 'eraser', hotkey: 'E' },
  { tool: 'superpixel', hotkey: 'X' },
  { tool: 'measure', hotkey: 'M' },
]
