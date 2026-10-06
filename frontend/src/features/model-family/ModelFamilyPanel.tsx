import { useId, useMemo, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { useModelFamily } from '@/api/queries'
import type { ModelFamilyVersion } from '@/api/types'
import { ErrorState } from '@/components/ErrorState'
import { Spinner } from '@/components/Spinner'
import {
  classDeltas,
  formatBytes,
  layoutFamily,
  readMetrics,
  type FamilyLayout,
  type VersionMetrics,
} from './family'

const NODE_W = 220
const NODE_H = 56
const COL_GAP = 76
const ROW_GAP = 18
const PAD = 8

function f1Text(value: number | null): string {
  return value === null ? '—' : value.toFixed(2)
}

function nodeLabel(version: ModelFamilyVersion): string {
  return `${version.model_name} v${version.version}`
}

/** SVG text does not wrap: cut a long model name but keep the version number. */
function fitLabel(version: ModelFamilyVersion, max: number): string {
  const suffix = ` v${version.version}`
  const room = max - suffix.length
  const name = version.model_name
  return `${name.length <= room ? name : `${name.slice(0, room - 1)}…`}${suffix}`
}

/** "v2" next to a version of the same model, the full label otherwise. */
function shortLabel(version: ModelFamilyVersion, other: ModelFamilyVersion): string {
  return version.model_id === other.model_id ? `v${version.version}` : nodeLabel(version)
}

function nodeSummary(metrics: VersionMetrics): string {
  const parts: string[] = []
  if (metrics.dtype) parts.push(metrics.dtype)
  if (metrics.sizeBytes !== null) parts.push(formatBytes(metrics.sizeBytes))
  if (metrics.f1 !== null) parts.push(`F1 ${f1Text(metrics.f1)}`)
  return parts.join(' · ')
}

interface GraphProps {
  layout: FamilyLayout
  modelId: string
  selectedId: string | null
  onSelect: (id: string) => void
}

/** Versions as boxes, one column per generation, edges labelled by derivation. */
function FamilyGraph({ layout, modelId, selectedId, onSelect }: GraphProps): JSX.Element {
  const { t } = useTranslation('admin')
  const position = new Map(
    layout.nodes.map((node) => [
      node.version.id,
      {
        x: PAD + node.column * (NODE_W + COL_GAP),
        y: PAD + node.row * (NODE_H + ROW_GAP),
      },
    ]),
  )
  const width = PAD * 2 + layout.columns * NODE_W + Math.max(0, layout.columns - 1) * COL_GAP
  const height = PAD * 2 + layout.rows * NODE_H + Math.max(0, layout.rows - 1) * ROW_GAP

  const onKey = (event: KeyboardEvent, id: string): void => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault()
      onSelect(id)
    }
  }

  return (
    <div className="overflow-x-auto">
      <svg
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        role="group"
        aria-label={t('models.family.graphLabel')}
      >
        {layout.edges.map((edge) => {
          const from = position.get(edge.from)
          const to = position.get(edge.to)
          if (!from || !to) return null
          const x1 = from.x + NODE_W
          const y1 = from.y + NODE_H / 2
          const x2 = to.x
          const y2 = to.y + NODE_H / 2
          const mid = (x1 + x2) / 2
          return (
            <g key={`${edge.from}-${edge.to}`}>
              <path
                d={`M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2 - 6},${y2}`}
                className="fill-none stroke-muted"
                strokeWidth={1.5}
              />
              <path
                d={`M${x2 - 6},${y2 - 4} L${x2},${y2} L${x2 - 6},${y2 + 4} Z`}
                className="fill-muted"
              />
              {edge.derivation && (
                <text
                  x={mid}
                  y={(y1 + y2) / 2 + 16}
                  textAnchor="middle"
                  className="fill-muted text-[11px]"
                >
                  {t(`models.family.derivation.${edge.derivation}`)}
                </text>
              )}
            </g>
          )
        })}
        {layout.nodes.map(({ version }) => {
          const at = position.get(version.id)
          if (!at) return null
          const metrics = readMetrics(version.metrics)
          const selected = version.id === selectedId
          const own = version.model_id === modelId
          return (
            <g
              key={version.id}
              transform={`translate(${at.x},${at.y})`}
              role="button"
              tabIndex={0}
              aria-pressed={selected}
              aria-label={`${nodeLabel(version)}. ${nodeSummary(metrics)}`}
              className="cursor-pointer focus:outline-none [&:focus-visible>rect]:stroke-accent"
              onClick={() => onSelect(version.id)}
              onKeyDown={(event) => onKey(event, version.id)}
            >
              <rect
                width={NODE_W}
                height={NODE_H}
                rx={6}
                className={
                  selected
                    ? 'fill-accent/10 stroke-accent'
                    : own
                      ? 'fill-surface stroke-ink/40'
                      : 'fill-surface stroke-line'
                }
                strokeWidth={selected ? 2 : 1}
              />
              <title>{nodeLabel(version)}</title>
              <text x={10} y={22} className="fill-ink text-[13px] font-medium">
                {fitLabel(version, 28)}
              </text>
              <text x={10} y={42} className="fill-muted text-[11px]">
                {nodeSummary(metrics) || t('models.family.noMetrics')}
              </text>
            </g>
          )
        })}
      </svg>
    </div>
  )
}

type Axis = 'size' | 'latency'

const CHART_W = 480
const CHART_H = 240
const MARGIN = { top: 12, right: 16, bottom: 36, left: 52 }

interface TradeoffProps {
  versions: ModelFamilyVersion[]
  selectedId: string | null
  onSelect: (id: string) => void
}

/** F1 against size or latency: up and to the left is better. */
function TradeoffChart({ versions, selectedId, onSelect }: TradeoffProps): JSX.Element {
  const { t } = useTranslation('admin')
  const [axis, setAxis] = useState<Axis>('size')
  const radioName = useId()
  const points = versions
    .map((version) => {
      const metrics = readMetrics(version.metrics)
      const x = axis === 'size' ? metrics.sizeBytes : metrics.latencyMs
      return { version, x, y: metrics.f1 }
    })
    .filter((p): p is { version: ModelFamilyVersion; x: number; y: number } => {
      return p.x !== null && p.y !== null
    })
  const byId = new Map(points.map((p) => [p.version.id, p]))

  const plotW = CHART_W - MARGIN.left - MARGIN.right
  const plotH = CHART_H - MARGIN.top - MARGIN.bottom
  const maxX = Math.max(1, ...points.map((p) => p.x)) * 1.1
  // F1 from just below the lowest version to 1: versions of one family sit
  // close together, and on a 0–1 axis their differences would vanish.
  const minY = Math.max(0, Math.floor((Math.min(1, ...points.map((p) => p.y)) - 0.05) * 20) / 20)
  const step = 1 - minY <= 0.25 ? 0.05 : 1 - minY <= 0.5 ? 0.1 : 0.25
  const yTicks: number[] = []
  for (let y = 1; y >= minY - 1e-9; y -= step) yTicks.push(Math.max(0, y))
  const sx = (x: number): number => MARGIN.left + (x / maxX) * plotW
  const sy = (y: number): number => MARGIN.top + ((1 - y) / (1 - minY || 1)) * plotH
  const xTick = (x: number): string => (axis === 'size' ? formatBytes(x) : `${Math.round(x)} ms`)

  return (
    <div>
      <div className="mb-1 flex items-center gap-3 text-xs">
        <span className="font-medium text-ink">{t('models.family.tradeoffHeading')}</span>
        <div role="radiogroup" aria-label={t('models.family.axisLabel')} className="flex gap-2">
          {(['size', 'latency'] as const).map((value) => (
            <label key={value} className="flex items-center gap-1 text-muted">
              <input
                type="radio"
                name={radioName}
                checked={axis === value}
                onChange={() => setAxis(value)}
              />
              {t(`models.family.axis.${value}`)}
            </label>
          ))}
        </div>
      </div>
      {points.length === 0 ? (
        <p className="text-xs text-muted">{t('models.family.tradeoffEmpty')}</p>
      ) : (
        <svg
          viewBox={`0 0 ${CHART_W} ${CHART_H}`}
          className="w-full max-w-xl"
          role="img"
          aria-label={t('models.family.tradeoffLabel', {
            axis: t(`models.family.axis.${axis}`),
          })}
        >
          {yTicks.map((y) => (
            <g key={y}>
              <line
                x1={MARGIN.left}
                x2={CHART_W - MARGIN.right}
                y1={sy(y)}
                y2={sy(y)}
                className="stroke-line"
              />
              <text
                x={MARGIN.left - 6}
                y={sy(y) + 4}
                textAnchor="end"
                className="fill-muted text-[10px]"
              >
                {y.toFixed(2)}
              </text>
            </g>
          ))}
          {[0, 0.5, 1].map((f) => (
            <text
              key={f}
              x={sx(f * (maxX / 1.1))}
              y={CHART_H - MARGIN.bottom + 16}
              textAnchor="middle"
              className="fill-muted text-[10px]"
            >
              {xTick(f * (maxX / 1.1))}
            </text>
          ))}
          <text
            x={MARGIN.left + plotW / 2}
            y={CHART_H - 4}
            textAnchor="middle"
            className="fill-muted text-[11px]"
          >
            {t(`models.family.axisTitle.${axis}`)}
          </text>
          <text
            x={10}
            y={MARGIN.top + plotH / 2}
            textAnchor="middle"
            transform={`rotate(-90 10 ${MARGIN.top + plotH / 2})`}
            className="fill-muted text-[11px]"
          >
            F1
          </text>
          {points.map((p) => {
            const parent = p.version.parent_version_id
              ? byId.get(p.version.parent_version_id)
              : undefined
            return parent ? (
              <line
                key={`l-${p.version.id}`}
                x1={sx(parent.x)}
                y1={sy(parent.y)}
                x2={sx(p.x)}
                y2={sy(p.y)}
                className="stroke-muted"
                strokeDasharray="4 3"
              />
            ) : null
          })}
          {points.map((p) => {
            const selected = p.version.id === selectedId
            // Labels on the right half go left of their point, never off the edge.
            const flip = sx(p.x) > MARGIN.left + plotW * 0.6
            return (
              <g
                key={p.version.id}
                className="cursor-pointer"
                onClick={() => onSelect(p.version.id)}
              >
                <title>{`${nodeLabel(p.version)}: F1 ${f1Text(p.y)}, ${xTick(p.x)}`}</title>
                <circle
                  cx={sx(p.x)}
                  cy={sy(p.y)}
                  r={selected ? 7 : 5}
                  className={selected ? 'fill-accent' : 'fill-ink/70'}
                />
                <text
                  x={sx(p.x) + (flip ? -9 : 9)}
                  y={sy(p.y) - 8}
                  textAnchor={flip ? 'end' : 'start'}
                  className="fill-ink text-[11px]"
                >
                  {fitLabel(p.version, 26)}
                </text>
              </g>
            )
          })}
        </svg>
      )}
    </div>
  )
}

interface ClassCompareProps {
  version: ModelFamilyVersion
  parent: ModelFamilyVersion | undefined
}

/** Per-class F1 of the selected version against its parent, biggest loss first. */
function ClassCompare({ version, parent }: ClassCompareProps): JSX.Element {
  const { t } = useTranslation('admin')
  if (!parent) {
    return <p className="text-xs text-muted">{t('models.family.noParent')}</p>
  }
  const rows = classDeltas(readMetrics(parent.metrics), readMetrics(version.metrics))
  if (rows.length === 0) {
    return <p className="text-xs text-muted">{t('models.family.noPerClass')}</p>
  }
  return (
    <table className="w-full max-w-xl text-xs">
      <caption className="mb-1 text-left font-medium text-ink">
        {t('models.family.compareHeading', {
          version: nodeLabel(version),
          parent: nodeLabel(parent),
        })}
      </caption>
      <thead>
        <tr className="text-left text-muted">
          <th className="py-1 pr-2 font-normal">{t('models.family.columnClass')}</th>
          <th className="py-1 pr-2 text-right font-normal">{shortLabel(parent, version)}</th>
          <th className="py-1 pr-2 text-right font-normal">{shortLabel(version, parent)}</th>
          <th className="w-1/2 py-1 font-normal">{t('models.family.columnChange')}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => {
          const delta = row.delta ?? 0
          const width = `${Math.min(50, Math.abs(delta) * 100)}%`
          const bar = delta < 0 ? 'right-1/2 bg-danger' : 'left-1/2 bg-success'
          const tone = delta < 0 ? 'text-danger' : 'text-ink'
          return (
            <tr key={row.name} className="border-t border-line">
              <td className="py-1 pr-2 text-ink">{row.name}</td>
              <td className="py-1 pr-2 text-right tabular-nums">{f1Text(row.before)}</td>
              <td className="py-1 pr-2 text-right tabular-nums">{f1Text(row.after)}</td>
              <td className="py-1">
                {row.delta === null ? (
                  <span className="text-muted">—</span>
                ) : (
                  <div className="flex items-center gap-2">
                    <div className="relative h-2 flex-1 rounded bg-line/50">
                      <div className="absolute inset-y-0 left-1/2 w-px bg-muted" />
                      <div
                        className={`absolute inset-y-0 rounded ${bar}`}
                        style={{ width }}
                      />
                    </div>
                    <span className={`w-12 text-right tabular-nums ${tone}`}>
                      {delta >= 0 ? '+' : ''}
                      {delta.toFixed(2)}
                    </span>
                  </div>
                )}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

interface ModelFamilyPanelProps {
  modelId: string
}

/**
 * How a model's versions were made (EXP-8): the derivation graph, an
 * accuracy-versus-cost chart and a per-class comparison with the parent.
 */
export function ModelFamilyPanel({ modelId }: ModelFamilyPanelProps): JSX.Element {
  const { t } = useTranslation(['admin', 'common'])
  const familyQuery = useModelFamily(modelId, true)
  const versions = useMemo(() => familyQuery.data?.versions ?? [], [familyQuery.data])
  const layout = useMemo(() => layoutFamily(versions), [versions])
  const [chosenId, setChosenId] = useState<string | null>(null)

  if (familyQuery.isLoading) {
    return <Spinner label={t('models.family.loading')} />
  }
  if (familyQuery.isError) {
    return (
      <ErrorState
        title={t('models.family.loadError')}
        message={familyQuery.error instanceof Error ? familyQuery.error.message : ''}
        onRetry={() => void familyQuery.refetch()}
      />
    )
  }
  if (versions.length === 0) {
    return <p className="text-sm text-muted">{t('models.versionsPanel.empty')}</p>
  }

  // Default to the newest derived version: the comparison worth seeing first.
  const derived = versions.filter((v) => v.parent_version_id)
  const selectedId = chosenId ?? derived.at(-1)?.id ?? versions.at(-1)?.id ?? null
  const selected = versions.find((v) => v.id === selectedId)
  const parent = selected?.parent_version_id
    ? versions.find((v) => v.id === selected.parent_version_id)
    : undefined

  return (
    <section className="space-y-4" aria-label={t('models.family.heading')}>
      <p className="text-xs text-muted">{t('models.family.hint')}</p>
      <FamilyGraph
        layout={layout}
        modelId={modelId}
        selectedId={selectedId}
        onSelect={setChosenId}
      />
      <TradeoffChart versions={versions} selectedId={selectedId} onSelect={setChosenId} />
      {selected && <ClassCompare version={selected} parent={parent} />}
    </section>
  )
}
