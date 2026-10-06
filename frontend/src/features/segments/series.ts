/**
 * Time-series CSV parsing (§5): a header row, the time axis in the first
 * column (numbers, or ISO 8601 timestamps read as epoch milliseconds), and
 * one numeric channel per further column. An empty cell is a gap.
 */

export interface Series {
  timeLabel: string
  /** True when the time column held timestamps (epoch milliseconds). */
  timestamps: boolean
  time: number[]
  channels: Array<{ name: string; values: Array<number | null> }>
}

export type SeriesResult = { ok: true; series: Series } | { ok: false; error: string }

/** Split one CSV line, honouring double-quoted fields with `""` escapes. */
export function splitCsvLine(line: string): string[] {
  const cells: string[] = []
  let cell = ''
  let quoted = false
  for (let i = 0; i < line.length; i += 1) {
    const char = line[i]
    if (quoted) {
      if (char === '"' && line[i + 1] === '"') {
        cell += '"'
        i += 1
      } else if (char === '"') {
        quoted = false
      } else {
        cell += char
      }
    } else if (char === '"') {
      quoted = true
    } else if (char === ',') {
      cells.push(cell)
      cell = ''
    } else {
      cell += char
    }
  }
  cells.push(cell)
  return cells.map((value) => value.trim())
}

function parseTime(raw: string): { value: number; timestamp: boolean } | null {
  if (raw === '') return null
  const number = Number(raw)
  if (Number.isFinite(number)) return { value: number, timestamp: false }
  const parsed = Date.parse(raw)
  return Number.isNaN(parsed) ? null : { value: parsed, timestamp: true }
}

export function parseSeries(csv: string): SeriesResult {
  const lines = csv.split(/\r?\n/).filter((line) => line.trim() !== '')
  if (lines.length < 2) return { ok: false, error: 'needs a header row and at least one row' }
  const header = splitCsvLine(lines[0] ?? '')
  if (header.length < 2) return { ok: false, error: 'needs a time column and at least one channel' }
  const names = header.slice(1)
  const time: number[] = []
  const values: Array<Array<number | null>> = names.map(() => [])
  let timestamps: boolean | null = null

  for (let row = 1; row < lines.length; row += 1) {
    const cells = splitCsvLine(lines[row] ?? '')
    const parsed = parseTime(cells[0] ?? '')
    if (parsed === null) return { ok: false, error: `row ${row + 1}: the time "${cells[0]}" is not a number or a date` }
    if (timestamps === null) timestamps = parsed.timestamp
    else if (timestamps !== parsed.timestamp) {
      return { ok: false, error: `row ${row + 1}: mixes numbers and dates in the time column` }
    }
    time.push(parsed.value)
    names.forEach((_, index) => {
      const raw = cells[index + 1] ?? ''
      const number = raw === '' ? null : Number(raw)
      values[index]?.push(number !== null && Number.isFinite(number) ? number : null)
    })
  }
  for (let i = 1; i < time.length; i += 1) {
    if ((time[i] ?? 0) < (time[i - 1] ?? 0)) {
      return { ok: false, error: `row ${i + 2}: time goes backwards` }
    }
  }
  return {
    ok: true,
    series: {
      timeLabel: header[0] ?? 'time',
      timestamps: timestamps ?? false,
      time,
      channels: names.map((name, index) => ({ name, values: values[index] ?? [] })),
    },
  }
}

/**
 * Downsample one channel to at most `buckets` points: each bucket keeps its
 * min and max (in time order), so spikes survive. Gaps are dropped.
 */
export function downsample(
  time: readonly number[],
  values: ReadonlyArray<number | null>,
  buckets: number,
): Array<[number, number]> {
  const points: Array<[number, number]> = []
  for (let i = 0; i < time.length; i += 1) {
    const value = values[i]
    if (value !== null && value !== undefined) points.push([time[i] ?? 0, value])
  }
  if (points.length <= buckets * 2) return points
  const size = points.length / buckets
  const out: Array<[number, number]> = []
  for (let bucket = 0; bucket < buckets; bucket += 1) {
    const slice = points.slice(Math.floor(bucket * size), Math.floor((bucket + 1) * size))
    if (slice.length === 0) continue
    let low = slice[0] as [number, number]
    let high = low
    for (const point of slice) {
      if (point[1] < low[1]) low = point
      if (point[1] > high[1]) high = point
    }
    out.push(...(low[0] <= high[0] ? [low, high] : [high, low]))
  }
  return out
}
