/**
 * Text annotator (TOOL): named-entity spans, overlapping and nested spans,
 * and directed relations between spans.
 *
 * Offsets are Unicode code points (see `offsets.ts`). Selecting text with the
 * mouse creates a span of the active class; clicking a highlighted run selects
 * the innermost span covering it. With a span selected, "Link" (R) starts a
 * relation of the active relation class and the next span clicked is its
 * target. Delete / Backspace removes the selection — a span takes its
 * relations with it.
 */

import { Fragment, useCallback, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import type { AnnotationResult, LabelClass, RelationShape, Shape, TextSpanShape } from '@/api/types'
import { useHotkeys, type HotkeyMap } from '@/lib/hotkeys'
import { newShapeId } from '@/lib/ids'

import {
  isRelation,
  isSpan,
  removeShape,
  segmentText,
  trimRange,
  utf16ToCodePoint,
} from './offsets'

export interface TextAnnotatorProps {
  text: string
  classes: LabelClass[]
  value: AnnotationResult
  onChange: (next: AnnotationResult) => void
  readOnly?: boolean
  onSelectionChange?: (id: string | null) => void
  /**
   * Code-point range of each page of a PDF taken in as text (`meta.pdf_text.pages`,
   * CONTRACTS "PDF text mode"); a "Page N" marker is shown where each begins.
   */
  pages?: number[][]
  /**
   * Called with a 1-based page when the person clicks its marker or selects a
   * span on it, so a PDF view beside the text can turn to that page.
   */
  onPageFocus?: (page: number) => void
}


/** UTF-16 offset of a DOM position, counted from the start of `container`. */
function domOffset(container: Node, node: Node, offset: number): number {
  const range = document.createRange()
  range.selectNodeContents(container)
  range.setEnd(node, offset)
  return range.toString().length
}

export function TextAnnotator({
  text,
  classes,
  value,
  onChange,
  readOnly = false,
  onSelectionChange,
  pages,
  onPageFocus,
}: TextAnnotatorProps): JSX.Element {
  const { t } = useTranslation('annotator')
  const spanClasses = useMemo(() => classes.filter((c) => c.tools.includes('span')), [classes])
  const relationClasses = useMemo(
    () => classes.filter((c) => c.tools.includes('relation')),
    [classes],
  )
  const colorOf = useMemo(() => {
    const map = new Map(classes.map((c) => [c.name, c.color]))
    return (name: string) => map.get(name) ?? '#94a3b8'
  }, [classes])
  const labelOf = useMemo(() => {
    const map = new Map(classes.map((c) => [c.name, c.display_name || c.name]))
    return (name: string) => map.get(name) ?? name
  }, [classes])

  const chars = useMemo(() => Array.from(text), [text])
  const spans = useMemo(() => value.shapes.filter(isSpan), [value.shapes])
  const relations = useMemo(() => value.shapes.filter(isRelation), [value.shapes])
  const segments = useMemo(
    () => splitAtPages(segmentText(chars.length, spans), pages),
    [chars.length, spans, pages],
  )
  const markersAt = useMemo(() => pageMarkers(pages, chars.length), [pages, chars.length])
  const marker = (position: number): JSX.Element[] =>
    (markersAt.get(position) ?? []).map(({ page, empty }) => {
      const label = empty ? t('text.pageEmpty', { page }) : t('text.page', { page })
      // The label is CSS content, not a text node, so DOM offsets (`domOffset`) skip it.
      return (
        <button
          key={`page-${page}`}
          type="button"
          aria-label={onPageFocus ? t('text.showPage', { page }) : label}
          data-testid="page-marker"
          data-label={label}
          disabled={!onPageFocus}
          onClick={() => onPageFocus?.(page)}
          className="block select-none font-sans text-xs text-muted before:content-[attr(data-label)]
            enabled:hover:text-accent enabled:hover:underline"
        />
      )
    })
  const spanById = useMemo(() => new Map(spans.map((s) => [s.id, s])), [spans])

  const [activeClass, setActiveClass] = useState<string | null>(null)
  const [activeRelation, setActiveRelation] = useState<string | null>(null)
  const [selectedId, setSelectedIdState] = useState<string | null>(null)
  const [linkFrom, setLinkFrom] = useState<string | null>(null)
  const bodyRef = useRef<HTMLDivElement>(null)

  const currentClass = activeClass ?? spanClasses[0]?.name ?? null
  const currentRelation = activeRelation ?? relationClasses[0]?.name ?? null

  const select = useCallback(
    (id: string | null) => {
      setSelectedIdState(id)
      onSelectionChange?.(id)
      const span = id ? spanById.get(id) : undefined
      const page = span ? pageOf(pages, span.start) : null
      if (page !== null) onPageFocus?.(page)
    },
    [onPageFocus, onSelectionChange, pages, spanById],
  )

  const setShapes = useCallback(
    (shapes: Shape[]) => onChange({ ...value, media_type: 'text', shapes }),
    [onChange, value],
  )

  const addSpan = useCallback(
    (start: number, end: number) => {
      if (!currentClass) return
      const span: TextSpanShape = {
        id: newShapeId(),
        type: 'span',
        class: currentClass,
        attributes: {},
        confidence: null,
        start,
        end,
        text: chars.slice(start, end).join(''),
      }
      setShapes([...value.shapes, span])
      select(span.id)
    },
    [chars, currentClass, select, setShapes, value.shapes],
  )

  const addRelation = useCallback(
    (from: string, to: string) => {
      if (!currentRelation || from === to) return
      const relation: RelationShape = {
        id: newShapeId(),
        type: 'relation',
        class: currentRelation,
        attributes: {},
        confidence: null,
        from,
        to,
      }
      setShapes([...value.shapes, relation])
      select(relation.id)
    },
    [currentRelation, select, setShapes, value.shapes],
  )

  const handleMouseUp = useCallback(() => {
    if (readOnly || linkFrom) return
    const body = bodyRef.current
    const selection = window.getSelection()
    if (!body || !selection || selection.rangeCount === 0 || selection.isCollapsed) return
    const range = selection.getRangeAt(0)
    if (!body.contains(range.startContainer) || !body.contains(range.endContainer)) return
    const from = utf16ToCodePoint(text, domOffset(body, range.startContainer, range.startOffset))
    const to = utf16ToCodePoint(text, domOffset(body, range.endContainer, range.endOffset))
    const trimmed = trimRange(chars, from, to)
    selection.removeAllRanges()
    if (trimmed) addSpan(trimmed[0], trimmed[1])
  }, [addSpan, chars, linkFrom, readOnly, text])

  const handleSegmentClick = useCallback(
    (covering: TextSpanShape[]) => {
      // A drag that ends on a span is a new span, not a click on the old one.
      const selection = window.getSelection()
      if (selection && !selection.isCollapsed) return
      const innermost = covering[covering.length - 1]
      if (!innermost) return
      if (linkFrom) {
        addRelation(linkFrom, innermost.id)
        setLinkFrom(null)
        return
      }
      select(innermost.id)
    },
    [addRelation, linkFrom, select],
  )

  const startLink = useCallback(() => {
    if (readOnly || !selectedId || !spanById.has(selectedId) || !currentRelation) return
    setLinkFrom(selectedId)
  }, [currentRelation, readOnly, selectedId, spanById])

  const deleteSelected = useCallback(() => {
    if (readOnly || !selectedId) return
    setShapes(removeShape(value.shapes, selectedId))
    select(null)
  }, [readOnly, select, selectedId, setShapes, value.shapes])

  const hotkeys = useMemo(() => {
    const map: HotkeyMap = {
      delete: deleteSelected,
      backspace: deleteSelected,
      escape: () => {
        setLinkFrom(null)
        select(null)
      },
      r: startLink,
    }
    for (const cls of spanClasses) {
      if (!cls.hotkey) continue
      map[cls.hotkey] = (event) => {
        // Consumed, so a class on N or P does not also change the item.
        if (!readOnly) event.preventDefault()
        setActiveClass(cls.name)
      }
    }
    return map
  }, [deleteSelected, readOnly, select, spanClasses, startLink])
  useHotkeys(hotkeys)

  const describeSpan = (id: string): string => {
    const span = spanById.get(id)
    if (!span) return '?'
    return `${labelOf(span.class)} “${chars.slice(span.start, span.end).join('')}”`
  }

  return (
    <div className="flex h-full w-full flex-col gap-3 text-ink">
      <div
        className="flex flex-wrap items-center gap-2"
        role="toolbar"
        aria-label={t('text.spanToolsAriaLabel')}
      >
        {spanClasses.length === 0 && (
          <p className="text-sm text-muted">{t('text.noSpanClass')}</p>
        )}
        {spanClasses.map((cls) => (
          <button
            key={cls.name}
            type="button"
            aria-pressed={cls.name === currentClass}
            onClick={() => setActiveClass(cls.name)}
            className={`flex items-center gap-1 rounded border px-2 py-1 text-sm ${
              cls.name === currentClass ? 'border-accent' : 'border-line'
            }`}
          >
            <span
              aria-hidden
              className="inline-block h-3 w-3 rounded-sm"
              style={{ backgroundColor: cls.color }}
            />
            {cls.display_name || cls.name}
            {cls.hotkey && <kbd className="text-xs text-muted">{cls.hotkey}</kbd>}
          </button>
        ))}
        {relationClasses.length > 0 && (
          <>
            <label className="ml-2 flex items-center gap-1 text-sm text-muted">
              {t('text.relation')}
              <select
                aria-label={t('text.relationClassAriaLabel')}
                className="rounded border border-line bg-surface px-1 py-0.5 text-sm text-ink"
                value={currentRelation ?? ''}
                onChange={(event) => setActiveRelation(event.target.value)}
              >
                {relationClasses.map((cls) => (
                  <option key={cls.name} value={cls.name}>
                    {cls.display_name || cls.name}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className="rounded border border-line px-2 py-1 text-sm disabled:opacity-50"
              disabled={readOnly || !selectedId || !spanById.has(selectedId)}
              onClick={startLink}
              title={t('text.linkTitle')}
            >
              {t('text.link')}
            </button>
          </>
        )}
      </div>

      {linkFrom && (
        <p role="status" className="text-sm text-accent">
          {t('text.linkingFrom', { span: describeSpan(linkFrom) })}
        </p>
      )}

      <div
        ref={bodyRef}
        data-testid="text-body"
        className="max-h-[70vh] flex-1 overflow-auto whitespace-pre-wrap rounded border border-line bg-surface p-4 font-mono text-sm leading-7"
        onMouseUp={handleMouseUp}
      >
        {segments.map((segment) => {
          const content = chars.slice(segment.start, segment.end).join('')
          if (segment.spans.length === 0) {
            return (
              <Fragment key={segment.start}>
                {marker(segment.start)}
                <span>{content}</span>
              </Fragment>
            )
          }
          const selected = segment.spans.some((s) => s.id === selectedId)
          // One underline per covering span, outermost lowest, so nested and
          // overlapping spans stay distinguishable.
          const shadow = segment.spans
            .map((s, i) => `inset 0 -${(segment.spans.length - i) * 3}px 0 0 ${colorOf(s.class)}`)
            .join(', ')
          const innermost = segment.spans[segment.spans.length - 1]
          return (
            <Fragment key={segment.start}>
              {marker(segment.start)}
              <span
                data-span-ids={segment.spans.map((s) => s.id).join(' ')}
                title={segment.spans.map((s) => labelOf(s.class)).join(' › ')}
                onClick={() => handleSegmentClick(segment.spans)}
                className={`cursor-pointer ${selected ? 'rounded-sm outline outline-1 outline-accent' : ''}`}
                style={{
                  boxShadow: shadow,
                  backgroundColor: `${colorOf(innermost.class)}22`,
                  paddingBottom: `${segment.spans.length * 3}px`,
                }}
              >
                {content}
              </span>
            </Fragment>
          )
        })}
        {marker(chars.length)}
      </div>

      {relations.length > 0 && (
        <section aria-label={t('text.relationsAriaLabel')}>
          <h3 className="mb-1 text-sm font-semibold">
            {t('text.relationsHeading', { count: relations.length })}
          </h3>
          <ul className="space-y-1 text-sm">
            {relations.map((relation) => (
              <li
                key={relation.id}
                className={`flex items-center justify-between gap-2 rounded px-2 py-1 ${
                  relation.id === selectedId ? 'bg-line/40' : ''
                }`}
              >
                <button type="button" className="text-left" onClick={() => select(relation.id)}>
                  {describeSpan(relation.from)}{' '}
                  <span style={{ color: colorOf(relation.class) }}>
                    —{labelOf(relation.class)}→
                  </span>{' '}
                  {describeSpan(relation.to)}
                </button>
                {!readOnly && (
                  <button
                    type="button"
                    aria-label={t('text.deleteRelation', { label: labelOf(relation.class) })}
                    className="text-xs text-muted hover:text-danger"
                    onClick={() => {
                      setShapes(removeShape(value.shapes, relation.id))
                      if (selectedId === relation.id) select(null)
                    }}
                  >
                    ✕
                  </button>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}


/** `segments` cut at every page start, so a page marker can sit between two of them. */
function splitAtPages<T extends { start: number; end: number }>(
  segments: T[],
  pages: number[][] | undefined,
): T[] {
  if (!pages || pages.length < 2) return segments
  const cuts = pages.map(([start]) => start)
  return segments.flatMap((segment) => {
    const inside = cuts.filter((cut) => cut > segment.start && cut < segment.end)
    if (inside.length === 0) return [segment]
    const bounds = [segment.start, ...[...new Set(inside)].sort((a, b) => a - b), segment.end]
    return bounds.slice(0, -1).map((start, i) => ({ ...segment, start, end: bounds[i + 1] }))
  })
}

/**
 * Where to show "Page N": at each page's first code point. Pages with no text
 * share the position of the next text, so several markers may sit together.
 * Nothing for a one-page document.
 */
function pageMarkers(
  pages: number[][] | undefined,
  length: number,
): Map<number, Array<{ page: number; empty: boolean }>> {
  const markers = new Map<number, Array<{ page: number; empty: boolean }>>()
  if (!pages || pages.length < 2) return markers
  pages.forEach(([start, end], index) => {
    const at = Math.min(start, length)
    const list = markers.get(at) ?? []
    list.push({ page: index + 1, empty: end <= start })
    markers.set(at, list)
  })
  return markers
}

/** The 1-based page whose range holds code point `position`, or null without pages. */
function pageOf(pages: number[][] | undefined, position: number): number | null {
  if (!pages || pages.length === 0) return null
  let found = 1
  pages.forEach(([start, end], index) => {
    if (end > start && start <= position) found = index + 1 // empty pages hold no text
  })
  return found
}
