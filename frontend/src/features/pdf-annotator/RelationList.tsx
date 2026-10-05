/** The relations of a pdf result, including those between shapes on different pages. */
import { useTranslation } from 'react-i18next'

import type { RelationShape } from '@/api/types'

interface RelationListProps {
  relations: RelationShape[]
  selectedId: string | null
  readOnly: boolean
  labelOf: (className: string) => string
  colorOf: (className: string) => string
  describe: (shapeId: string) => string
  onSelect: (id: string) => void
  onDelete: (id: string) => void
}

export function RelationList({
  relations,
  selectedId,
  readOnly,
  labelOf,
  colorOf,
  describe,
  onSelect,
  onDelete,
}: RelationListProps): JSX.Element {
  const { t } = useTranslation('annotator')
  return (
    <section aria-label={t('pdf.relation.listLabel')} className="w-full max-w-xl text-sm text-ink">
      <h3 className="mb-1 font-semibold">
        {t('pdf.relation.heading', { count: relations.length })}
      </h3>
      <ul className="space-y-1">
        {relations.map((relation) => (
          <li
            key={relation.id}
            className={`flex items-center justify-between gap-2 rounded px-2 py-1 ${
              relation.id === selectedId ? 'bg-line/40' : ''
            }`}
          >
            <button type="button" className="text-left" onClick={() => onSelect(relation.id)}>
              {describe(relation.from)}{' '}
              <span style={{ color: colorOf(relation.class) }}>—{labelOf(relation.class)}→</span>{' '}
              {describe(relation.to)}
            </button>
            {!readOnly && (
              <button
                type="button"
                aria-label={t('pdf.relation.delete', { label: labelOf(relation.class) })}
                className="text-xs text-muted hover:text-danger"
                onClick={() => onDelete(relation.id)}
              >
                ✕
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}
