import { describe, expect, it } from 'vitest'
import type { ModelFamilyVersion } from '@/api/types'
import { classDeltas, formatBytes, layoutFamily, readMetrics } from './family'

function version(
  id: string,
  overrides: Partial<ModelFamilyVersion> = {},
): ModelFamilyVersion {
  return {
    id,
    model_id: 'm1',
    model_name: 'detector',
    model_task: 'detect',
    version: 1,
    class_mapping: {},
    metrics: {},
    snapshot_id: null,
    snapshot_digest: null,
    training_run: null,
    parent_version_id: null,
    derivation: null,
    created_at: '2026-10-01T00:00:00Z',
    ...overrides,
  }
}

describe('layoutFamily', () => {
  it('puts each generation one column right of its parent', () => {
    const layout = layoutFamily([
      version('teacher'),
      version('student', {
        parent_version_id: 'teacher',
        derivation: 'distilled',
        created_at: '2026-10-02T00:00:00Z',
      }),
      version('int8', {
        parent_version_id: 'student',
        derivation: 'quantized',
        created_at: '2026-10-03T00:00:00Z',
      }),
      version('fp16', {
        parent_version_id: 'student',
        derivation: 'quantized',
        created_at: '2026-10-04T00:00:00Z',
      }),
    ])

    const at = Object.fromEntries(layout.nodes.map((n) => [n.version.id, [n.column, n.row]]))
    expect(at).toEqual({ teacher: [0, 0], student: [1, 0], int8: [2, 0], fp16: [2, 1] })
    expect(layout.columns).toBe(3)
    expect(layout.rows).toBe(2)
    expect(layout.edges).toContainEqual({ from: 'student', to: 'int8', derivation: 'quantized' })
    expect(layout.edges).toHaveLength(3)
  })

  it('makes a version whose parent is not in the family a root, with no edge', () => {
    const layout = layoutFamily([version('orphan', { parent_version_id: 'gone' })])
    expect(layout.nodes[0]).toMatchObject({ column: 0, row: 0 })
    expect(layout.edges).toEqual([])
  })

  it('survives a parent cycle in bad data', () => {
    const layout = layoutFamily([
      version('a', { parent_version_id: 'b' }),
      version('b', { parent_version_id: 'a' }),
    ])
    expect(layout.nodes).toHaveLength(2)
  })
})

describe('readMetrics', () => {
  it('reads the well-known keys and ignores anything malformed', () => {
    const metrics = readMetrics({
      f1: 0.8,
      size_bytes: 1000,
      latency_ms_p50: 12,
      dtype: 'int8',
      precision: 'high',
      per_class: { car: { f1: 0.9, precision: 1, recall: 0.8 }, bad: 3 },
    })
    expect(metrics).toMatchObject({
      f1: 0.8,
      sizeBytes: 1000,
      latencyMs: 12,
      dtype: 'int8',
      precision: null,
    })
    expect(Object.keys(metrics.perClass)).toEqual(['car'])
  })
})

describe('classDeltas', () => {
  it('lists the biggest loss first and classes missing on one side last', () => {
    const parent = readMetrics({
      per_class: { car: { f1: 0.9 }, bus: { f1: 0.8 }, bike: { f1: 0.5 } },
    })
    const child = readMetrics({
      per_class: { car: { f1: 0.88 }, bus: { f1: 0.6 }, truck: { f1: 0.4 } },
    })
    const rows = classDeltas(parent, child)
    expect(rows.map((r) => r.name)).toEqual(['bus', 'car', 'bike', 'truck'])
    expect(rows[0].delta).toBeCloseTo(-0.2)
    expect(rows[2]).toMatchObject({ before: 0.5, after: null, delta: null })
  })
})

describe('formatBytes', () => {
  it.each([
    [512, '512 B'],
    [19_799_685, '19.8 MB'],
    [76_001_132, '76.0 MB'],
    [250_000_000, '250 MB'],
  ])('%d → %s', (bytes, text) => {
    expect(formatBytes(bytes)).toBe(text)
  })
})
