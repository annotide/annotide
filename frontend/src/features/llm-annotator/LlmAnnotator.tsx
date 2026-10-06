/**
 * LLM evaluation annotator (§5 LLM-data): compare and rank candidate
 * responses, score them (or whole conversation turns) on a scale, and judge
 * a conversation overall through the schema's `classification` (rendered by
 * the page, not here).
 *
 * Conversation-level judgements are the existing `classification`; a
 * rationale is a class attribute, edited through the existing
 * `AttributeEditor` once its shape is selected (`onSelectionChange`).
 */

import { useCallback, useMemo } from 'react'
import { useTranslation } from 'react-i18next'

import type {
  AnnotationResult,
  LabelClass,
  LlmDocument,
  RankingShape,
  RatingShape,
  Scale,
  Shape,
} from '@/api/types'
import { newShapeId } from '@/lib/ids'

import {
  CONVERSATION_TARGET,
  effectiveOrder,
  messageTarget,
  moveInOrder,
  ratingFor,
  responseTarget,
  removeRating,
  upsertRanking,
  upsertRating,
} from './shapes'

export interface LlmAnnotatorProps {
  document: LlmDocument
  classes: LabelClass[]
  value: AnnotationResult
  onChange: (next: AnnotationResult) => void
  readOnly?: boolean
  onSelectionChange?: (id: string | null) => void
}

/** A, B, C, … — short, stable labels for responses that don't name themselves. */
function responseLabel(index: number): string {
  return String.fromCharCode(65 + index)
}

export function LlmAnnotator({
  document,
  classes,
  value,
  onChange,
  readOnly = false,
  onSelectionChange,
}: LlmAnnotatorProps): JSX.Element {
  const { t } = useTranslation('annotator')
  const rankingClasses = useMemo(() => classes.filter((c) => c.tools.includes('ranking')), [classes])
  const ratingClasses = useMemo(() => classes.filter((c) => c.tools.includes('rating')), [classes])
  const labelOf = useMemo(() => {
    const map = new Map(classes.map((c) => [c.name, c.display_name || c.name]))
    return (name: string) => map.get(name) ?? name
  }, [classes])
  const responseIds = useMemo(() => document.responses.map((r) => r.id), [document.responses])
  const responseIndex = useMemo(
    () => new Map(document.responses.map((r, i) => [r.id, i])),
    [document.responses],
  )

  const select = useCallback((id: string | null) => onSelectionChange?.(id), [onSelectionChange])

  const setShapes = useCallback(
    (shapes: Shape[]) => onChange({ ...value, media_type: 'llm', shapes }),
    [onChange, value],
  )

  const setRanking = useCallback(
    (cls: string, order: string[]) => {
      if (readOnly) return
      const existing = value.shapes.find((s) => s.type === 'ranking' && s.class === cls) as
        | RankingShape
        | undefined
      const shape: RankingShape = existing
        ? { ...existing, order }
        : { id: newShapeId(), type: 'ranking', class: cls, attributes: {}, confidence: null, order }
      setShapes(upsertRanking(value.shapes, shape))
      select(shape.id)
    },
    [readOnly, select, setShapes, value.shapes],
  )

  const setRating = useCallback(
    (cls: string, target: string, next: number | undefined) => {
      if (readOnly) return
      if (next === undefined) {
        setShapes(removeRating(value.shapes, cls, target))
        return
      }
      const existing = ratingFor(value.shapes, cls, target)
      const shape: RatingShape = existing
        ? { ...existing, value: next }
        : {
            id: newShapeId(),
            type: 'rating',
            class: cls,
            attributes: {},
            confidence: null,
            target,
            value: next,
          }
      setShapes(upsertRating(value.shapes, shape))
      select(shape.id)
    },
    [readOnly, select, setShapes, value.shapes],
  )

  return (
    <div className="flex h-full w-full min-w-0 flex-col gap-4 overflow-y-auto text-ink lg:flex-row">
      <section
        aria-label={t('llm.conversationHeading')}
        className="min-w-0 flex-1 overflow-y-auto rounded border border-line bg-surface p-3"
      >
        <h3 className="mb-2 text-sm font-semibold text-ink">{t('llm.conversationHeading')}</h3>
        {document.messages.length === 0 ? (
          <p className="text-sm text-muted">{t('llm.noMessages')}</p>
        ) : (
          <ul className="space-y-3">
            {document.messages.map((message, index) => {
              const target = messageTarget(index)
              return (
                <li key={index} className="min-w-0 rounded border border-line/60 p-2">
                  <p className="mb-1 text-xs font-semibold uppercase text-muted">
                    {t(`llm.role.${message.role}`, { defaultValue: message.role })}
                  </p>
                  <p className="whitespace-pre-wrap break-words text-sm">{message.content}</p>
                  {message.role === 'assistant' && ratingClasses.length > 0 && (
                    <div className="mt-2 space-y-2 border-t border-line/60 pt-2">
                      {ratingClasses.map((cls) => (
                        <ScaleControl
                          key={cls.name}
                          label={t('llm.messageRatingLabel', {
                            index,
                            label: labelOf(cls.name),
                          })}
                          scale={cls.scale ?? { min: 1, max: 5 }}
                          value={ratingFor(value.shapes, cls.name, target)?.value}
                          onChange={(next) => setRating(cls.name, target, next)}
                          readOnly={readOnly}
                          idPrefix={`llm-rating-${cls.name}-${target}`}
                        />
                      ))}
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        )}
      </section>

      <section
        aria-label={t('llm.responsesHeading')}
        className="min-w-0 flex-1 overflow-y-auto rounded border border-line bg-surface p-3"
      >
        <h3 className="mb-2 text-sm font-semibold text-ink">{t('llm.responsesHeading')}</h3>

        {rankingClasses.length > 0 && document.responses.length > 0 && (
          <div className="mb-4 space-y-3">
            {rankingClasses.map((cls) => (
              <RankingControl
                key={cls.name}
                label={labelOf(cls.name)}
                responses={document.responses}
                order={effectiveOrder(value.shapes, cls.name, responseIds)}
                readOnly={readOnly}
                onChange={(order) => setRanking(cls.name, order)}
                onSelect={() => {
                  const existing = value.shapes.find(
                    (s) => s.type === 'ranking' && s.class === cls.name,
                  )
                  if (existing) select(existing.id)
                }}
              />
            ))}
          </div>
        )}

        {document.responses.length === 0 ? (
          <p className="text-sm text-muted">{t('llm.noResponses')}</p>
        ) : (
          <ul className="space-y-3">
            {document.responses.map((responseItem) => {
              const index = responseIndex.get(responseItem.id) ?? 0
              const target = responseTarget(responseItem.id)
              return (
                <li key={responseItem.id} className="min-w-0 rounded border border-line/60 p-2">
                  <p className="mb-1 text-xs font-semibold text-muted">
                    {t('llm.responseLabel', { label: responseLabel(index) })}
                    {responseItem.model ? ` · ${responseItem.model}` : ''}
                  </p>
                  <p className="whitespace-pre-wrap break-words text-sm">{responseItem.content}</p>
                  {ratingClasses.length > 0 && (
                    <div className="mt-2 space-y-2 border-t border-line/60 pt-2">
                      {ratingClasses.map((cls) => (
                        <ScaleControl
                          key={cls.name}
                          label={t('llm.responseRatingLabel', {
                            response: t('llm.responseLabel', { label: responseLabel(index) }),
                            label: labelOf(cls.name),
                          })}
                          scale={cls.scale ?? { min: 1, max: 5 }}
                          value={ratingFor(value.shapes, cls.name, target)?.value}
                          onChange={(next) => setRating(cls.name, target, next)}
                          readOnly={readOnly}
                          idPrefix={`llm-rating-${cls.name}-${target}`}
                        />
                      ))}
                    </div>
                  )}
                </li>
              )
            })}
          </ul>
        )}

        {document.responses.length === 0 && ratingClasses.length > 0 && (
          <div className="mt-4 space-y-2 border-t border-line/60 pt-2">
            {ratingClasses.map((cls) => (
              <ScaleControl
                key={cls.name}
                label={t('llm.conversationRatingLabel', { label: labelOf(cls.name) })}
                scale={cls.scale ?? { min: 1, max: 5 }}
                value={ratingFor(value.shapes, cls.name, CONVERSATION_TARGET)?.value}
                onChange={(next) => setRating(cls.name, CONVERSATION_TARGET, next)}
                readOnly={readOnly}
                idPrefix={`llm-rating-${cls.name}-conversation`}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  )
}

interface RankingControlProps {
  label: string
  responses: LlmDocument['responses']
  order: string[]
  readOnly: boolean
  onChange: (order: string[]) => void
  onSelect: () => void
}

function RankingControl({
  label,
  responses,
  order,
  readOnly,
  onChange,
  onSelect,
}: RankingControlProps): JSX.Element {
  const { t } = useTranslation('annotator')
  const byId = useMemo(() => new Map(responses.map((r, i) => [r.id, i])), [responses])
  const labelFor = (id: string) => responseLabel(byId.get(id) ?? 0)

  if (responses.length === 2) {
    const [first, second] = responses
    const preferredFirst = order[0] === first.id
    return (
      <fieldset
        aria-label={t('llm.preferenceHeading', { label })}
        className="rounded border border-line/60 p-2"
      >
        <legend className="text-xs font-medium text-muted">
          {t('llm.preferenceHeading', { label })}
        </legend>
        <div className="mt-1 flex gap-2">
          {[first, second].map((responseItem, index) => {
            const chosen = index === 0 ? preferredFirst : !preferredFirst
            return (
              <button
                key={responseItem.id}
                type="button"
                disabled={readOnly}
                aria-pressed={order.length > 0 && chosen}
                onClick={() => {
                  onChange([responseItem.id, index === 0 ? second.id : first.id])
                  onSelect()
                }}
                className={`rounded border px-2 py-1 text-sm disabled:opacity-50 ${
                  order.length > 0 && chosen ? 'border-accent' : 'border-line'
                }`}
              >
                {t('llm.preferenceOption', { label: responseLabel(index) })}
              </button>
            )
          })}
        </div>
      </fieldset>
    )
  }

  return (
    <fieldset
      aria-label={t('llm.rankingHeading', { label })}
      className="rounded border border-line/60 p-2"
    >
      <legend className="text-xs font-medium text-muted">
        {t('llm.rankingHeading', { label })}
      </legend>
      <ol className="mt-1 space-y-1">
        {order.map((id, index) => {
          const responseName = t('llm.responseLabel', { label: labelFor(id) })
          return (
            <li key={id} className="flex items-center gap-2 text-sm">
              <span className="text-xs text-muted">
                {t('llm.rankPosition', { rank: index + 1 })}
              </span>
              <button
                type="button"
                className="flex-1 text-left"
                disabled={readOnly}
                onClick={onSelect}
              >
                {responseName}
              </button>
              <button
                type="button"
                aria-label={t('llm.moveUp', { label: responseName })}
                disabled={readOnly || index === 0}
                onClick={() => {
                  onChange(moveInOrder(order, index, -1))
                  onSelect()
                }}
                className="rounded border border-line px-1.5 py-0.5 text-xs disabled:opacity-40"
              >
                ↑
              </button>
              <button
                type="button"
                aria-label={t('llm.moveDown', { label: responseName })}
                disabled={readOnly || index === order.length - 1}
                onClick={() => {
                  onChange(moveInOrder(order, index, 1))
                  onSelect()
                }}
                className="rounded border border-line px-1.5 py-0.5 text-xs disabled:opacity-40"
              >
                ↓
              </button>
            </li>
          )
        })}
      </ol>
    </fieldset>
  )
}

interface ScaleControlProps {
  label: string
  scale: Scale
  value: number | undefined
  onChange: (value: number | undefined) => void
  readOnly: boolean
  idPrefix: string
}

function ScaleControl({
  label,
  scale,
  value,
  onChange,
  readOnly,
  idPrefix,
}: ScaleControlProps): JSX.Element {
  const { t } = useTranslation('annotator')
  const options: number[] = []
  for (let n = scale.min; n <= scale.max; n += 1) options.push(n)

  return (
    <fieldset className="text-sm text-ink">
      <legend className="text-xs font-medium text-muted">{label}</legend>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        {options.map((n) => {
          const id = `${idPrefix}-${n}`
          return (
            <label key={n} htmlFor={id} className="flex items-center gap-1 text-xs">
              <input
                id={id}
                type="radio"
                name={idPrefix}
                checked={value === n}
                disabled={readOnly}
                onChange={() => onChange(n)}
              />
              {scale.labels?.[String(n)] ?? n}
            </label>
          )
        })}
        {!readOnly && value !== undefined && (
          <button
            type="button"
            className="text-xs text-muted hover:text-danger"
            aria-label={t('llm.clearRating', { label })}
            onClick={() => onChange(undefined)}
          >
            ✕
          </button>
        )}
      </div>
    </fieldset>
  )
}
