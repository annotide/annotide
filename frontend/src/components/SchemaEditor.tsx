import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useCreateSchemaVersion, useSchemas } from '@/api/queries'
import { ApiError } from '@/api/client'
import type {
  AttributeType,
  ClassificationField,
  LabelAttribute,
  LabelClass,
  Scale,
  ShapeTool,
  Skeleton,
} from '@/api/types'
import { Button } from '@/components/Button'
import i18n from '@/i18n'
import { Spinner } from '@/components/Spinner'
import { ErrorState } from '@/components/ErrorState'

const SHAPE_TOOLS: ShapeTool[] = [
  'bbox',
  'rbox',
  'polygon',
  'polyline',
  'point',
  'mask',
  'keypoints',
  'classification',
  'ranking',
  'rating',
  'segment',
]
const ATTRIBUTE_TYPES: AttributeType[] = ['text', 'number', 'select', 'multiselect', 'boolean']
const DEFAULT_SCALE: Scale = { min: 1, max: 5 }

function needsOptions(type: AttributeType): boolean {
  return type === 'select' || type === 'multiselect'
}

function emptyClass(): LabelClass {
  return { name: '', display_name: '', color: '#3b82f6', hotkey: undefined, tools: [], attributes: [] }
}

function emptyAttribute(): LabelAttribute {
  return { name: '', type: 'text', required: false, options: undefined }
}

function emptyClassificationField(): ClassificationField {
  return { name: '', type: 'text', required: false, options: undefined }
}

/**
 * Validates the schema per TOOL-1/TOOL-2 rules the backend enforces: unique
 * class names, unique hotkeys, at least one tool per class, and `options`
 * required for select/multiselect attributes (and forbidden otherwise).
 * Returns a list of human-readable problems, empty when the schema is valid.
 */
export function validateSchema(
  classes: LabelClass[],
  classification: ClassificationField[],
): string[] {
  const errors: string[] = []
  const seenNames = new Set<string>()
  const seenHotkeys = new Set<string>()

  if (classes.length === 0) {
    errors.push(i18n.t('settings:schema.validation.noClasses'))
  }

  for (const cls of classes) {
    if (!cls.name.trim()) {
      errors.push(i18n.t('settings:schema.validation.classNeedsName'))
    } else if (seenNames.has(cls.name)) {
      errors.push(i18n.t('settings:schema.validation.duplicateClass', { name: cls.name }))
    } else {
      seenNames.add(cls.name)
    }

    if (cls.tools.length === 0) {
      errors.push(i18n.t('settings:schema.validation.needsTool', { name: cls.name || i18n.t('settings:schema.validation.unnamed') }))
    }

    if (cls.tools.includes('keypoints')) {
      const points = cls.skeleton?.points ?? []
      if (points.length === 0) {
        errors.push(
          i18n.t('settings:schema.validation.needsKeypoint', { name: cls.name || i18n.t('settings:schema.validation.unnamed') }),
        )
      } else if (new Set(points).size !== points.length) {
        errors.push(
          i18n.t('settings:schema.validation.duplicateKeypoint', { name: cls.name || i18n.t('settings:schema.validation.unnamed') }),
        )
      }
    }

    if (cls.tools.includes('rating')) {
      const name = cls.name || i18n.t('settings:schema.validation.unnamed')
      if (!cls.scale) {
        errors.push(i18n.t('settings:schema.validation.needsScale', { name }))
      } else if (!Number.isInteger(cls.scale.min) || !Number.isInteger(cls.scale.max)) {
        errors.push(i18n.t('settings:schema.validation.scaleNotInteger', { name }))
      } else if (cls.scale.min >= cls.scale.max) {
        errors.push(i18n.t('settings:schema.validation.scaleMinMax', { name }))
      } else if (cls.scale.max - cls.scale.min + 1 > 21) {
        errors.push(i18n.t('settings:schema.validation.scaleTooManySteps', { name }))
      }
    }

    if (cls.hotkey) {
      if (seenHotkeys.has(cls.hotkey)) {
        errors.push(i18n.t('settings:schema.validation.duplicateHotkey', { hotkey: cls.hotkey }))
      } else {
        seenHotkeys.add(cls.hotkey)
      }
    }

    for (const attr of cls.attributes) {
      errors.push(
        ...validateAttributeLike(
          attr,
          i18n.t('settings:schema.validation.classContext', { name: cls.name || i18n.t('settings:schema.validation.unnamed') }),
        ),
      )
    }
  }

  for (const field of classification) {
    errors.push(
      ...validateAttributeLike(field, i18n.t('settings:schema.validation.classificationContext')),
    )
  }

  return errors
}

function validateAttributeLike(
  attr: LabelAttribute | ClassificationField,
  context: string,
): string[] {
  const errors: string[] = []
  if (!attr.name.trim()) {
    errors.push(i18n.t('settings:schema.validation.attrNoName', { subject: context }))
  }
  const hasOptions = Boolean(attr.options && attr.options.length > 0)
  if (needsOptions(attr.type) && !hasOptions) {
    errors.push(
      i18n.t('settings:schema.validation.attrNeedsOption', {
        subject: context,
        name: attr.name || i18n.t('settings:schema.validation.unnamed'),
      }),
    )
  }
  if (!needsOptions(attr.type) && hasOptions) {
    errors.push(
      i18n.t('settings:schema.validation.attrNoOptions', {
        subject: context,
        name: attr.name || i18n.t('settings:schema.validation.unnamed'),
      }),
    )
  }
  return errors
}

/** `head-left_hand` style bones, one per comma. A name may itself contain
 * `-`: each split is tried until both halves name a point. */
export function parseBones(
  text: string,
  points: string[],
): { edges: [number, number][]; unknown: string[] } {
  const edges: [number, number][] = []
  const unknown: string[] = []
  for (const bone of text.split(',').map((b) => b.trim()).filter(Boolean)) {
    let edge: [number, number] | null = null
    for (let at = bone.indexOf('-'); at > 0; at = bone.indexOf('-', at + 1)) {
      const a = points.indexOf(bone.slice(0, at).trim())
      const b = points.indexOf(bone.slice(at + 1).trim())
      if (a >= 0 && b >= 0 && a !== b) {
        edge = [a, b]
        break
      }
    }
    const duplicate =
      edge && edges.some(([a, b]) => (a === edge[0] && b === edge[1]) || (a === edge[1] && b === edge[0]))
    if (!edge) unknown.push(bone)
    else if (!duplicate) edges.push(edge)
  }
  return { edges, unknown }
}

export function formatBones(skeleton: Skeleton): string {
  return skeleton.edges.map(([a, b]) => `${skeleton.points[a]}-${skeleton.points[b]}`).join(', ')
}

/** New point names, keeping every bone whose two ends still exist (by name). */
export function renameSkeletonPoints(skeleton: Skeleton, points: string[]): Skeleton {
  const edges: [number, number][] = []
  for (const [a, b] of skeleton.edges) {
    const from = points.indexOf(skeleton.points[a])
    const to = points.indexOf(skeleton.points[b])
    if (from >= 0 && to >= 0) edges.push([from, to])
  }
  return { points, edges }
}

function parseOptions(text: string): string[] | undefined {
  const options = text
    .split(',')
    .map((option) => option.trim())
    .filter(Boolean)
  return options.length > 0 ? options : undefined
}

export interface SchemaEditorProps {
  projectId: string
  /** Hides every write control; the caller decides based on the owner check (SEC-3). */
  readOnly?: boolean
}

/** Editor for a project's label schema. Versions are immutable: saving always
 * creates the next one (TOOL-4). */
export function SchemaEditor({ projectId, readOnly = false }: SchemaEditorProps): JSX.Element {
  const { t } = useTranslation(['settings', 'common'])
  const schemasQuery = useSchemas(projectId)
  const createVersion = useCreateSchemaVersion(projectId)

  const [classes, setClasses] = useState<LabelClass[]>([])
  const [classification, setClassification] = useState<ClassificationField[]>([])
  const [errors, setErrors] = useState<string[]>([])
  // Bones that name no keypoint, per class index; they block saving.
  const [boneErrors, setBoneErrors] = useState<Record<number, string>>({})
  const seeded = useRef(false)

  const versions = schemasQuery.data ?? []
  const latest = versions[0]
  const nextVersion = (latest?.version ?? 0) + 1

  useEffect(() => {
    if (seeded.current) return
    if (!schemasQuery.data) return
    seeded.current = true
    setClasses(latest?.definition.classes ?? [])
    setClassification(latest?.definition.classification ?? [])
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [schemasQuery.data])

  function updateClass(index: number, patch: Partial<LabelClass>): void {
    setClasses((prev) => prev.map((cls, i) => (i === index ? { ...cls, ...patch } : cls)))
  }

  function toggleTool(index: number, tool: ShapeTool): void {
    setClasses((prev) =>
      prev.map((cls, i) => {
        if (i !== index) return cls
        const has = cls.tools.includes(tool)
        const tools = has ? cls.tools.filter((t) => t !== tool) : [...cls.tools, tool]
        // The backend requires a skeleton/scale with the tool and forbids it without.
        if (tool === 'keypoints') {
          return { ...cls, tools, skeleton: has ? undefined : { points: [], edges: [] } }
        }
        if (tool === 'rating') {
          return { ...cls, tools, scale: has ? undefined : { ...DEFAULT_SCALE } }
        }
        return { ...cls, tools }
      }),
    )
  }

  function updateScale(index: number, patch: Partial<Scale>): void {
    setClasses((prev) =>
      prev.map((cls, i) => (i === index ? { ...cls, scale: { ...(cls.scale ?? DEFAULT_SCALE), ...patch } } : cls)),
    )
  }

  function updateScaleLabel(index: number, bound: 'min' | 'max', text: string): void {
    setClasses((prev) =>
      prev.map((cls, i) => {
        if (i !== index) return cls
        const scale = cls.scale ?? DEFAULT_SCALE
        const key = String(bound === 'min' ? scale.min : scale.max)
        const labels = { ...scale.labels }
        if (text) labels[key] = text
        else delete labels[key]
        return { ...cls, scale: { ...scale, labels: Object.keys(labels).length > 0 ? labels : undefined } }
      }),
    )
  }

  function updateAttribute(classIndex: number, attrIndex: number, patch: Partial<LabelAttribute>): void {
    setClasses((prev) =>
      prev.map((cls, i) => {
        if (i !== classIndex) return cls
        return {
          ...cls,
          attributes: cls.attributes.map((attr, j) => (j === attrIndex ? { ...attr, ...patch } : attr)),
        }
      }),
    )
  }

  function updateClassificationField(index: number, patch: Partial<ClassificationField>): void {
    setClassification((prev) => prev.map((field, i) => (i === index ? { ...field, ...patch } : field)))
  }

  function updatePoints(index: number, text: string): void {
    const points = text
      .split(',')
      .map((name) => name.trim())
      .filter(Boolean)
    setClasses((prev) =>
      prev.map((cls, i) =>
        i === index
          ? { ...cls, skeleton: renameSkeletonPoints(cls.skeleton ?? { points: [], edges: [] }, points) }
          : cls,
      ),
    )
  }

  function updateBones(index: number, text: string): void {
    const skeleton = classes[index]?.skeleton ?? { points: [], edges: [] }
    const { edges, unknown } = parseBones(text, skeleton.points)
    setBoneErrors((prev) => {
      const next = { ...prev }
      if (unknown.length > 0) next[index] = t('schema.unknownBones', { bones: unknown.join(', ') })
      else delete next[index]
      return next
    })
    if (unknown.length === 0) updateClass(index, { skeleton: { ...skeleton, edges } })
  }

  function handleSave(): void {
    const problems = [...validateSchema(classes, classification), ...Object.values(boneErrors)]
    setErrors(problems)
    if (problems.length > 0) return
    createVersion.mutate({ version: nextVersion, classes, classification })
  }

  return (
    <section aria-labelledby="schema-heading" className="mb-8">
      <h2 id="schema-heading" className="mb-3 text-lg font-semibold text-ink">
        {t('schema.heading')}
      </h2>

      {schemasQuery.isLoading && (
        <div className="flex justify-center py-6">
          <Spinner label={t('schema.loading')} />
        </div>
      )}

      {schemasQuery.isError && (
        <ErrorState
          title={t('schema.loadError')}
          message={
            schemasQuery.error instanceof ApiError
              ? (schemasQuery.error.detail ?? schemasQuery.error.title)
              : t('schema.unknownError')
          }
          onRetry={() => void schemasQuery.refetch()}
        />
      )}

      {versions.length > 0 && (
        <ul className="mb-4 space-y-1 text-sm text-muted">
          {versions.map((version) => (
            <li key={version.id}>
              v{version.version} · {new Date(version.created_at).toLocaleString()}
            </li>
          ))}
        </ul>
      )}

      <div className="space-y-4">
        {classes.map((cls, classIndex) => (
          <div key={classIndex} className="rounded-md border border-line p-3">
            <div className="flex flex-wrap items-end gap-3">
              <label className="flex flex-col gap-1 text-sm">
                {t('schema.nameLabel')}
                <input
                  aria-label={`class-name-${classIndex}`}
                  value={cls.name}
                  disabled={readOnly}
                  onChange={(event) => updateClass(classIndex, { name: event.target.value })}
                  className="rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink"
                />
              </label>
              <label className="flex flex-col gap-1 text-sm">
                {t('schema.displayNameLabel')}
                <input
                  aria-label={`class-display-name-${classIndex}`}
                  value={cls.display_name}
                  disabled={readOnly}
                  onChange={(event) => updateClass(classIndex, { display_name: event.target.value })}
                  className="rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink"
                />
              </label>
              <label className="flex flex-col gap-1 text-sm">
                {t('schema.colorLabel')}
                <input
                  type="color"
                  aria-label={`class-color-${classIndex}`}
                  value={cls.color}
                  disabled={readOnly}
                  onChange={(event) => updateClass(classIndex, { color: event.target.value })}
                />
              </label>
              <label className="flex flex-col gap-1 text-sm">
                {t('schema.hotkeyLabel')}
                <input
                  aria-label={`class-hotkey-${classIndex}`}
                  value={cls.hotkey ?? ''}
                  maxLength={1}
                  disabled={readOnly}
                  onChange={(event) =>
                    updateClass(classIndex, { hotkey: event.target.value || undefined })
                  }
                  className="w-12 rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink"
                />
              </label>
              {!readOnly && (
                <Button
                  variant="danger"
                  size="sm"
                  onClick={() => {
                    setClasses((prev) => prev.filter((_, i) => i !== classIndex))
                    setBoneErrors({})
                  }}
                >
                  {t('schema.removeClass')}
                </Button>
              )}
            </div>

            <fieldset className="mt-3">
              <legend className="text-xs font-medium text-muted">{t('schema.toolsLegend')}</legend>
              <div className="mt-1 flex flex-wrap gap-3">
                {SHAPE_TOOLS.map((tool) => (
                  <label key={tool} className="flex items-center gap-1 text-sm text-ink">
                    <input
                      type="checkbox"
                      checked={cls.tools.includes(tool)}
                      disabled={readOnly}
                      onChange={() => toggleTool(classIndex, tool)}
                    />
                    {tool}
                  </label>
                ))}
              </div>
            </fieldset>

            {cls.tools.includes('keypoints') && (
              <fieldset className="mt-3 space-y-2">
                <legend className="text-xs font-medium text-muted">{t('schema.skeletonLegend')}</legend>
                <label className="block text-sm text-ink">
                  <span className="text-xs text-muted">{t('schema.keypointsHint')}</span>
                  <input
                    // Remount on outside changes; edits apply on blur so typing is not reparsed.
                    key={`points-${(cls.skeleton?.points ?? []).join(',')}`}
                    aria-label={`class-${classIndex}-keypoints`}
                    placeholder={t('schema.keypointsPlaceholder')}
                    defaultValue={(cls.skeleton?.points ?? []).join(', ')}
                    disabled={readOnly}
                    onBlur={(event) => updatePoints(classIndex, event.target.value)}
                    className="mt-1 w-full rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink"
                  />
                </label>
                <label className="block text-sm text-ink">
                  <span className="text-xs text-muted">{t('schema.bonesHint')}</span>
                  <input
                    key={`bones-${cls.skeleton ? formatBones(cls.skeleton) : ''}`}
                    aria-label={`class-${classIndex}-bones`}
                    placeholder={t('schema.bonesPlaceholder')}
                    defaultValue={cls.skeleton ? formatBones(cls.skeleton) : ''}
                    disabled={readOnly}
                    onBlur={(event) => updateBones(classIndex, event.target.value)}
                    className="mt-1 w-full rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink"
                  />
                </label>
                {boneErrors[classIndex] && (
                  <p role="alert" className="text-xs text-danger">
                    {boneErrors[classIndex]}
                  </p>
                )}
              </fieldset>
            )}

            {cls.tools.includes('rating') && (
              <fieldset className="mt-3 flex flex-wrap items-end gap-3">
                <legend className="text-xs font-medium text-muted">{t('schema.scaleLegend')}</legend>
                {(['min', 'max'] as const).map((bound) => {
                  const scale = cls.scale ?? DEFAULT_SCALE
                  return (
                    <div key={bound} className="flex items-end gap-2">
                      <label className="flex flex-col text-xs text-muted">
                        {t(bound === 'min' ? 'schema.scaleMin' : 'schema.scaleMax')}
                        <input
                          type="number"
                          step={1}
                          aria-label={`class-${classIndex}-scale-${bound}`}
                          value={scale[bound]}
                          disabled={readOnly}
                          onChange={(event) =>
                            updateScale(classIndex, { [bound]: Number(event.target.value) })
                          }
                          className="mt-1 w-20 rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink"
                        />
                      </label>
                      <label className="flex flex-col text-xs text-muted">
                        {t(bound === 'min' ? 'schema.scaleMinLabel' : 'schema.scaleMaxLabel')}
                        <input
                          aria-label={`class-${classIndex}-scale-${bound}-label`}
                          placeholder={t(
                            bound === 'min' ? 'schema.scaleMinPlaceholder' : 'schema.scaleMaxPlaceholder',
                          )}
                          value={scale.labels?.[String(scale[bound])] ?? ''}
                          disabled={readOnly}
                          onChange={(event) => updateScaleLabel(classIndex, bound, event.target.value)}
                          className="mt-1 w-36 rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink"
                        />
                      </label>
                    </div>
                  )
                })}
              </fieldset>
            )}

            <div className="mt-3">
              <p className="text-xs font-medium text-muted">{t('schema.attributesLabel')}</p>
              <div className="mt-1 space-y-2">
                {cls.attributes.map((attr, attrIndex) => (
                  <div key={attrIndex} className="flex flex-wrap items-end gap-2">
                    <input
                      aria-label={`class-${classIndex}-attribute-${attrIndex}-name`}
                      placeholder={t('schema.namePlaceholder')}
                      value={attr.name}
                      disabled={readOnly}
                      onChange={(event) =>
                        updateAttribute(classIndex, attrIndex, { name: event.target.value })
                      }
                      className="rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink"
                    />
                    <select
                      aria-label={`class-${classIndex}-attribute-${attrIndex}-type`}
                      value={attr.type}
                      disabled={readOnly}
                      onChange={(event) =>
                        updateAttribute(classIndex, attrIndex, {
                          type: event.target.value as AttributeType,
                        })
                      }
                      className="rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink"
                    >
                      {ATTRIBUTE_TYPES.map((type) => (
                        <option key={type} value={type}>
                          {type}
                        </option>
                      ))}
                    </select>
                    <label className="flex items-center gap-1 text-sm text-ink">
                      <input
                        type="checkbox"
                        checked={attr.required}
                        disabled={readOnly}
                        onChange={(event) =>
                          updateAttribute(classIndex, attrIndex, { required: event.target.checked })
                        }
                      />
                      {t('schema.required')}
                    </label>
                    {needsOptions(attr.type) && (
                      <input
                        aria-label={`class-${classIndex}-attribute-${attrIndex}-options`}
                        placeholder={t('schema.optionsPlaceholder')}
                        value={attr.options?.join(',') ?? ''}
                        disabled={readOnly}
                        onChange={(event) =>
                          updateAttribute(classIndex, attrIndex, {
                            options: parseOptions(event.target.value),
                          })
                        }
                        className="rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink"
                      />
                    )}
                    {!readOnly && (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() =>
                          setClasses((prev) =>
                            prev.map((c, i) =>
                              i === classIndex
                                ? { ...c, attributes: c.attributes.filter((_, j) => j !== attrIndex) }
                                : c,
                            ),
                          )
                        }
                      >
                        {t('schema.removeAttribute')}
                      </Button>
                    )}
                  </div>
                ))}
              </div>
              {!readOnly && (
                <Button
                  variant="secondary"
                  size="sm"
                  className="mt-2"
                  onClick={() =>
                    setClasses((prev) =>
                      prev.map((c, i) =>
                        i === classIndex ? { ...c, attributes: [...c.attributes, emptyAttribute()] } : c,
                      ),
                    )
                  }
                >
                  {t('schema.addAttribute')}
                </Button>
              )}
            </div>
          </div>
        ))}
      </div>

      {!readOnly && (
        <Button
          variant="secondary"
          size="sm"
          className="mt-3"
          onClick={() => setClasses((prev) => [...prev, emptyClass()])}
        >
          {t('schema.addClass')}
        </Button>
      )}

      <div className="mt-6">
        <p className="mb-2 text-sm font-semibold text-ink">{t('schema.classificationHeading')}</p>
        <div className="space-y-2">
          {classification.map((field, index) => (
            <div key={index} className="flex flex-wrap items-end gap-2">
              <input
                aria-label={`classification-${index}-name`}
                placeholder={t('schema.namePlaceholder')}
                value={field.name}
                disabled={readOnly}
                onChange={(event) => updateClassificationField(index, { name: event.target.value })}
                className="rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink"
              />
              <select
                aria-label={`classification-${index}-type`}
                value={field.type}
                disabled={readOnly}
                onChange={(event) =>
                  updateClassificationField(index, { type: event.target.value as AttributeType })
                }
                className="rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink"
              >
                {ATTRIBUTE_TYPES.map((type) => (
                  <option key={type} value={type}>
                    {type}
                  </option>
                ))}
              </select>
              <label className="flex items-center gap-1 text-sm text-ink">
                <input
                  type="checkbox"
                  checked={field.required}
                  disabled={readOnly}
                  onChange={(event) =>
                    updateClassificationField(index, { required: event.target.checked })
                  }
                />
                {t('schema.required')}
              </label>
              {needsOptions(field.type) && (
                <input
                  aria-label={`classification-${index}-options`}
                  placeholder={t('schema.optionsPlaceholder')}
                  value={field.options?.join(',') ?? ''}
                  disabled={readOnly}
                  onChange={(event) =>
                    updateClassificationField(index, { options: parseOptions(event.target.value) })
                  }
                  className="rounded-md border border-line bg-surface px-2 py-1 text-sm text-ink"
                />
              )}
              {!readOnly && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setClassification((prev) => prev.filter((_, i) => i !== index))}
                >
                  {t('schema.removeField')}
                </Button>
              )}
            </div>
          ))}
        </div>
        {!readOnly && (
          <Button
            variant="secondary"
            size="sm"
            className="mt-2"
            onClick={() => setClassification((prev) => [...prev, emptyClassificationField()])}
          >
            {t('schema.addClassificationField')}
          </Button>
        )}
      </div>

      {errors.length > 0 && (
        <ul role="alert" className="mt-4 space-y-1 text-sm text-danger">
          {errors.map((error) => (
            <li key={error}>{error}</li>
          ))}
        </ul>
      )}

      {createVersion.isError && (
        <p className="mt-3 text-sm text-danger">
          {createVersion.error instanceof ApiError
            ? (createVersion.error.detail ?? createVersion.error.title)
            : t('schema.saveError')}
        </p>
      )}

      {createVersion.isSuccess && (
        <p className="mt-3 text-sm text-success" role="status">
          {t('schema.savedVersion', { version: createVersion.data.version })}
        </p>
      )}

      {!readOnly && (
        <Button
          variant="primary"
          className="mt-4"
          disabled={createVersion.isPending}
          onClick={handleSave}
        >
          {createVersion.isPending
            ? t('common:saving')
            : t('schema.saveAsVersion', { version: nextVersion })}
        </Button>
      )}
    </section>
  )
}
