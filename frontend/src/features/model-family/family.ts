/**
 * Pure helpers for the model family views (EXP-8): reading the well-known
 * metric keys, laying out the derivation graph and comparing a version with
 * its parent class by class. No React, so they are tested on their own.
 */
import type { ModelDerivation, ModelFamilyVersion } from '@/api/types'

export interface ClassScore {
  precision: number | null
  recall: number | null
  f1: number | null
}

/** The metric keys the training pipeline writes (docs/CONTRACTS.md), when present. */
export interface VersionMetrics {
  f1: number | null
  precision: number | null
  recall: number | null
  sizeBytes: number | null
  latencyMs: number | null
  dtype: string | null
  perClass: Record<string, ClassScore>
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export function readMetrics(metrics: Record<string, unknown>): VersionMetrics {
  const perClass: Record<string, ClassScore> = {}
  const raw = metrics.per_class
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    for (const [name, value] of Object.entries(raw as Record<string, unknown>)) {
      if (!value || typeof value !== 'object') continue
      const score = value as Record<string, unknown>
      perClass[name] = {
        precision: num(score.precision),
        recall: num(score.recall),
        f1: num(score.f1),
      }
    }
  }
  return {
    f1: num(metrics.f1),
    precision: num(metrics.precision),
    recall: num(metrics.recall),
    sizeBytes: num(metrics.size_bytes),
    latencyMs: num(metrics.latency_ms_p50),
    dtype: typeof metrics.dtype === 'string' ? metrics.dtype : null,
    perClass,
  }
}

export interface FamilyNode {
  version: ModelFamilyVersion
  /** Generations from the root: a version is always right of its parent. */
  column: number
  row: number
}

export interface FamilyEdge {
  from: string
  to: string
  derivation: ModelDerivation | null
}

export interface FamilyLayout {
  nodes: FamilyNode[]
  edges: FamilyEdge[]
  columns: number
  rows: number
}

/**
 * Columns by generation, rows by creation time within a column. A parent
 * outside the family (another organisation's, or deleted) makes a root.
 */
export function layoutFamily(versions: ModelFamilyVersion[]): FamilyLayout {
  const byId = new Map(versions.map((v) => [v.id, v]))
  const depth = new Map<string, number>()
  const depthOf = (version: ModelFamilyVersion): number => {
    const known = depth.get(version.id)
    if (known !== undefined) return known
    // Parents exist before their children, so the chain ends; the guard is
    // for data that says otherwise.
    const seen = new Set<string>()
    let current: ModelFamilyVersion | undefined = version
    let hops = 0
    while (current?.parent_version_id && !seen.has(current.id)) {
      seen.add(current.id)
      current = byId.get(current.parent_version_id)
      if (current) hops += 1
    }
    depth.set(version.id, hops)
    return hops
  }

  const columns = new Map<number, ModelFamilyVersion[]>()
  for (const version of versions) {
    const column = depthOf(version)
    columns.set(column, [...(columns.get(column) ?? []), version])
  }
  const nodes: FamilyNode[] = []
  let rows = 0
  for (const [column, members] of columns) {
    members.sort((a, b) => a.created_at.localeCompare(b.created_at))
    members.forEach((version, row) => nodes.push({ version, column, row }))
    rows = Math.max(rows, members.length)
  }
  const edges = versions
    .filter((v) => v.parent_version_id && byId.has(v.parent_version_id))
    .map((v) => ({ from: v.parent_version_id as string, to: v.id, derivation: v.derivation }))
  return { nodes, edges, columns: columns.size, rows }
}

export interface ClassDelta {
  name: string
  before: number | null
  after: number | null
  /** after − before, null when either side has no F1 for the class. */
  delta: number | null
}

/** Per-class F1 of a version against its parent, biggest loss first. */
export function classDeltas(parent: VersionMetrics, child: VersionMetrics): ClassDelta[] {
  const names = new Set([...Object.keys(parent.perClass), ...Object.keys(child.perClass)])
  const rows = [...names].map((name) => {
    const before = parent.perClass[name]?.f1 ?? null
    const after = child.perClass[name]?.f1 ?? null
    const delta = before !== null && after !== null ? after - before : null
    return { name, before, after, delta }
  })
  return rows.sort((a, b) => {
    if (a.delta === null || b.delta === null) {
      return a.delta === null ? (b.delta === null ? a.name.localeCompare(b.name) : 1) : -1
    }
    return a.delta - b.delta || a.name.localeCompare(b.name)
  })
}

export function formatBytes(bytes: number): string {
  const units = ['B', 'kB', 'MB', 'GB']
  let value = bytes
  let unit = 0
  while (value >= 1000 && unit < units.length - 1) {
    value /= 1000
    unit += 1
  }
  return `${value >= 100 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`
}
