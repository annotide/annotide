/**
 * Form for the attribute values of one shape, or the item-level
 * classification, driven by the label schema (TOOL-2).
 *
 * A field's definition (`LabelAttribute` on a class, `ClassificationField` on
 * the schema) decides the control: text → input, number → number input,
 * select → dropdown, multiselect → checkbox group, boolean → checkbox. Values
 * are kept in the shape the backend validates (`validate_against_schema`,
 * QA-6): numbers as numbers, multiselect as `string[]`, and a cleared field is
 * removed from the map rather than stored as `""`/`null`, so a required field
 * that is emptied fails validation instead of passing with junk.
 */

import { useTranslation } from 'react-i18next'
import type { AttributeType, AttributeValue } from '@/api/types'

export interface AttributeField {
  name: string
  type: AttributeType
  required: boolean
  options?: string[]
}

export interface AttributeEditorProps {
  fields: AttributeField[]
  values: Record<string, AttributeValue>
  /** `undefined` clears the field. */
  onChange: (name: string, value: AttributeValue | undefined) => void
  readOnly?: boolean
  /** Keeps ids unique when two editors (classification + shape) share a page. */
  idPrefix?: string
}

const CONTROL_CLASS = 'rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink'

function labelFor(field: AttributeField): string {
  return field.required ? `${field.name} *` : field.name
}

export function AttributeEditor({
  fields,
  values,
  onChange,
  readOnly = false,
  idPrefix = 'attr',
}: AttributeEditorProps) {
  const { t } = useTranslation('annotator')
  if (fields.length === 0) {
    return <p className="text-sm text-muted">{t('attributes.none')}</p>
  }

  return (
    <div className="space-y-2">
      {fields.map((field) => {
        const id = `${idPrefix}-${field.name}`
        const value = values[field.name]
        const missing = field.required && value === undefined

        switch (field.type) {
          case 'boolean':
            return (
              <label key={field.name} htmlFor={id} className="flex items-center gap-2 text-sm text-ink">
                <input
                  id={id}
                  type="checkbox"
                  checked={value === true}
                  disabled={readOnly}
                  onChange={(event) => onChange(field.name, event.target.checked)}
                />
                {labelFor(field)}
              </label>
            )
          case 'select':
            return (
              <label key={field.name} htmlFor={id} className="flex flex-col gap-1 text-sm text-ink">
                {labelFor(field)}
                <select
                  id={id}
                  value={typeof value === 'string' ? value : ''}
                  disabled={readOnly}
                  aria-invalid={missing || undefined}
                  onChange={(event) =>
                    onChange(field.name, event.target.value === '' ? undefined : event.target.value)
                  }
                  className={CONTROL_CLASS}
                >
                  <option value="">—</option>
                  {(field.options ?? []).map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>
            )
          case 'multiselect': {
            const selected = Array.isArray(value) ? value : []
            return (
              <fieldset key={field.name} className="text-sm text-ink">
                <legend className="mb-1">{labelFor(field)}</legend>
                <div className="flex flex-wrap gap-x-3 gap-y-1">
                  {(field.options ?? []).map((option) => (
                    <label
                      key={option}
                      htmlFor={`${id}-${option}`}
                      className="flex items-center gap-1"
                    >
                      <input
                        id={`${id}-${option}`}
                        type="checkbox"
                        checked={selected.includes(option)}
                        disabled={readOnly}
                        onChange={(event) => {
                          const next = event.target.checked
                            ? [...selected, option]
                            : selected.filter((v) => v !== option)
                          onChange(field.name, next.length === 0 ? undefined : next)
                        }}
                      />
                      {option}
                    </label>
                  ))}
                </div>
              </fieldset>
            )
          }
          case 'number':
            return (
              <label key={field.name} htmlFor={id} className="flex flex-col gap-1 text-sm text-ink">
                {labelFor(field)}
                <input
                  id={id}
                  type="number"
                  value={typeof value === 'number' ? value : ''}
                  disabled={readOnly}
                  aria-invalid={missing || undefined}
                  onChange={(event) => {
                    const raw = event.target.value
                    onChange(field.name, raw === '' ? undefined : Number(raw))
                  }}
                  className={CONTROL_CLASS}
                />
              </label>
            )
          case 'text':
          default:
            return (
              <label key={field.name} htmlFor={id} className="flex flex-col gap-1 text-sm text-ink">
                {labelFor(field)}
                <input
                  id={id}
                  type="text"
                  value={typeof value === 'string' ? value : ''}
                  disabled={readOnly}
                  aria-invalid={missing || undefined}
                  onChange={(event) =>
                    onChange(field.name, event.target.value === '' ? undefined : event.target.value)
                  }
                  className={CONTROL_CLASS}
                />
              </label>
            )
        }
      })}
    </div>
  )
}
