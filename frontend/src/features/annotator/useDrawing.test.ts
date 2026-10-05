import { act, renderHook } from '@testing-library/react'
import { StrictMode } from 'react'
import { describe, expect, it, vi } from 'vitest'
import {
  createHistory,
  defaultAttributes,
  pushHistory,
  reconcileAttributes,
  redoHistory,
  undoHistory,
  useDrawing,
} from './useDrawing'
import type { AnnotationResult, LabelClass } from './types'

const classes: LabelClass[] = [
  {
    name: 'car',
    display_name: 'Car',
    color: '#e11d48',
    hotkey: '1',
    tools: ['bbox', 'polygon', 'point'],
    attributes: [],
  },
  {
    name: 'road',
    display_name: 'Road',
    color: '#22c55e',
    hotkey: '2',
    tools: ['polygon'],
    attributes: [],
  },
]

function emptyResult(): AnnotationResult {
  return { schema_version: 1, media_type: 'image', classification: {}, shapes: [] }
}

function setup(initial: AnnotationResult = emptyResult(), readOnly = false) {
  const onChange = vi.fn()
  const { result } = renderHook(() =>
    useDrawing({ value: initial, onChange, classes, imageWidth: 1000, imageHeight: 800, readOnly }),
  )
  return { result, onChange }
}

function lastValue(onChange: ReturnType<typeof vi.fn>): AnnotationResult {
  const calls = onChange.mock.calls as AnnotationResult[][]
  return calls[calls.length - 1][0]
}

describe('history stack (pure, generic)', () => {
  it('pushes, undoes and redoes', () => {
    let h = createHistory('a')
    h = pushHistory(h, 'b')
    h = pushHistory(h, 'c')
    expect(h.present).toBe('c')
    h = undoHistory(h)
    expect(h.present).toBe('b')
    h = undoHistory(h)
    expect(h.present).toBe('a')

    const atStart = h
    h = undoHistory(h) // no-op past the beginning
    expect(h).toBe(atStart)

    h = redoHistory(h)
    expect(h.present).toBe('b')
  })

  it('drops the redo branch on a fresh push after undo', () => {
    let h = createHistory('a')
    h = pushHistory(h, 'b')
    h = undoHistory(h)
    h = pushHistory(h, 'c')
    expect(h.present).toBe('c')
    expect(h.future).toEqual([])
    h = redoHistory(h) // 'b' was discarded, this is a no-op
    expect(h.present).toBe('c')
  })
})

describe('bbox tool: drag-direction normalisation', () => {
  it('normalises a top-left -> bottom-right drag', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('bbox')
      result.current.beginBBoxDrag([10, 20])
      result.current.updateBBoxDrag([110, 220])
      result.current.commitBBoxDrag()
    })
    const shape = lastValue(onChange).shapes[0]
    expect(shape.type).toBe('bbox')
    if (shape.type === 'bbox') expect(shape.bbox).toEqual([10, 20, 110, 220])
  })

  it('normalises a bottom-right -> top-left drag', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('bbox')
      result.current.beginBBoxDrag([110, 220])
      result.current.updateBBoxDrag([10, 20])
      result.current.commitBBoxDrag()
    })
    const shape = lastValue(onChange).shapes[0]
    if (shape.type === 'bbox') expect(shape.bbox).toEqual([10, 20, 110, 220])
  })

  it('normalises a top-right -> bottom-left drag', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('bbox')
      result.current.beginBBoxDrag([110, 20])
      result.current.updateBBoxDrag([10, 220])
      result.current.commitBBoxDrag()
    })
    const shape = lastValue(onChange).shapes[0]
    if (shape.type === 'bbox') expect(shape.bbox).toEqual([10, 20, 110, 220])
  })

  it('normalises a bottom-left -> top-right drag', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('bbox')
      result.current.beginBBoxDrag([10, 220])
      result.current.updateBBoxDrag([110, 20])
      result.current.commitBBoxDrag()
    })
    const shape = lastValue(onChange).shapes[0]
    if (shape.type === 'bbox') expect(shape.bbox).toEqual([10, 20, 110, 220])
    expect(result.current.draft).toBeNull()
    expect(result.current.selectedId).toBe(shape.id)
  })

  it('rejects a degenerate (near-zero-size) box and does not commit', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('bbox')
      result.current.beginBBoxDrag([10, 10])
      result.current.updateBBoxDrag([11, 11])
      result.current.commitBBoxDrag()
    })
    expect(onChange).not.toHaveBeenCalled()
    expect(result.current.draft).toBeNull()
    expect(result.current.canUndo).toBe(false)
  })

  it('clamps the committed box to image bounds', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('bbox')
      result.current.beginBBoxDrag([-50, -50])
      result.current.updateBBoxDrag([5000, 5000])
      result.current.commitBBoxDrag()
    })
    const shape = lastValue(onChange).shapes[0]
    if (shape.type === 'bbox') expect(shape.bbox).toEqual([0, 0, 1000, 800])
  })
})

describe('polygon tool', () => {
  it('requires at least 3 points to close', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('polygon')
      result.current.addPolygonPoint([0, 0])
      result.current.addPolygonPoint([10, 0])
      result.current.closePolygon()
    })
    expect(onChange).not.toHaveBeenCalled()
    expect(result.current.draft).toEqual({
      tool: 'polygon',
      points: [
        [0, 0],
        [10, 0],
      ],
    })
  })

  it('closes with 3 or more points and commits a polygon shape', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('polygon')
      result.current.addPolygonPoint([0, 0])
      result.current.addPolygonPoint([10, 0])
      result.current.addPolygonPoint([10, 10])
      result.current.closePolygon()
    })
    expect(onChange).toHaveBeenCalledTimes(1)
    const shape = lastValue(onChange).shapes[0]
    expect(shape.type).toBe('polygon')
    if (shape.type === 'polygon') {
      expect(shape.points).toEqual([
        [0, 0],
        [10, 0],
        [10, 10],
      ])
    }
    expect(result.current.draft).toBeNull()
  })

  it('Escape/cancel clears the draft without committing', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('polygon')
      result.current.addPolygonPoint([0, 0])
      result.current.addPolygonPoint([10, 0])
      result.current.cancelDraft()
    })
    expect(onChange).not.toHaveBeenCalled()
    expect(result.current.draft).toBeNull()
  })

  it('backspace removes only the last vertex', () => {
    const { result } = setup()
    act(() => {
      result.current.setTool('polygon')
      result.current.addPolygonPoint([0, 0])
      result.current.addPolygonPoint([10, 0])
      result.current.addPolygonPoint([10, 10])
      result.current.removeLastPolygonPoint()
    })
    expect(result.current.draft).toEqual({
      tool: 'polygon',
      points: [
        [0, 0],
        [10, 0],
      ],
    })
  })

  it('backspace on an empty draft is a no-op', () => {
    const { result } = setup()
    act(() => {
      result.current.setTool('polygon')
      result.current.removeLastPolygonPoint()
    })
    expect(result.current.draft).toBeNull()
  })
})

describe('polyline tool', () => {
  it('needs at least 2 points to finish', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('polyline')
      result.current.addPolylinePoint([0, 0])
      result.current.finishPolyline()
    })
    expect(onChange).not.toHaveBeenCalled()
    expect(result.current.draft).toEqual({ tool: 'polyline', points: [[0, 0]] })
  })

  it('commits an open path with 2 or more points', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('polyline')
      result.current.addPolylinePoint([0, 0])
      result.current.addPolylinePoint([10, 5])
      result.current.addPolylinePoint([20, 0])
      result.current.finishPolyline()
    })
    expect(onChange).toHaveBeenCalledTimes(1)
    const shape = lastValue(onChange).shapes[0]
    expect(shape.type).toBe('polyline')
    if (shape.type === 'polyline') {
      expect(shape.points).toEqual([
        [0, 0],
        [10, 5],
        [20, 0],
      ])
    }
    expect(result.current.selectedId).toBe(shape.id)
    expect(result.current.draft).toBeNull()
  })

  it('drops the duplicate vertex a finishing double-click leaves behind', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('polyline')
      result.current.addPolylinePoint([0, 0])
      result.current.addPolylinePoint([10, 5])
      result.current.addPolylinePoint([10, 5])
      result.current.finishPolyline()
    })
    const shape = lastValue(onChange).shapes[0]
    if (shape.type === 'polyline') {
      expect(shape.points).toEqual([
        [0, 0],
        [10, 5],
      ])
    }
  })

  it('backspace drops the last point and an empty draft is cleared', () => {
    const { result } = setup()
    act(() => {
      result.current.setTool('polyline')
      result.current.addPolylinePoint([0, 0])
      result.current.addPolylinePoint([10, 0])
      result.current.removeLastPolygonPoint()
    })
    expect(result.current.draft).toEqual({ tool: 'polyline', points: [[0, 0]] })
    act(() => result.current.removeLastPolygonPoint())
    expect(result.current.draft).toBeNull()
  })
})

describe('rotated box tool', () => {
  it('commits on the third click with the geometry of the three points', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('rbox')
      result.current.addRBoxPoint([100, 100])
      result.current.updateRBoxPreview([150, 100])
      result.current.addRBoxPoint([200, 100])
    })
    expect(onChange).not.toHaveBeenCalled()
    expect(result.current.draft).toEqual({
      tool: 'rbox',
      points: [
        [100, 100],
        [200, 100],
      ],
      current: [200, 100],
    })

    act(() => result.current.addRBoxPoint([120, 140]))
    expect(onChange).toHaveBeenCalledTimes(1)
    const shape = lastValue(onChange).shapes[0]
    expect(shape.type).toBe('rbox')
    if (shape.type === 'rbox') {
      expect(shape.center).toEqual([150, 120])
      expect(shape.size).toEqual([100, 40])
      expect(shape.angle).toBe(0)
    }
    expect(result.current.selectedId).toBe(shape.id)
    expect(result.current.draft).toBeNull()
  })

  it('rejects a degenerate third click and keeps the draft', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('rbox')
      result.current.addRBoxPoint([100, 100])
      result.current.addRBoxPoint([200, 100])
      result.current.addRBoxPoint([150, 101]) // 1 px deep
    })
    expect(onChange).not.toHaveBeenCalled()
    expect(result.current.draft?.tool).toBe('rbox')
  })

  it('a second click on the first point is ignored', () => {
    const { result } = setup()
    act(() => {
      result.current.setTool('rbox')
      result.current.addRBoxPoint([100, 100])
      result.current.addRBoxPoint([100, 100])
    })
    expect(result.current.draft).toEqual({
      tool: 'rbox',
      points: [[100, 100]],
      current: [100, 100],
    })
  })

  it('clamps clicks to the image', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('rbox')
      result.current.addRBoxPoint([-50, 0])
      result.current.addRBoxPoint([100, 0])
      result.current.addRBoxPoint([0, 2000])
    })
    const shape = lastValue(onChange).shapes[0]
    if (shape.type === 'rbox') {
      expect(shape.center).toEqual([50, 400])
      expect(shape.size).toEqual([100, 800])
    }
  })

  it('updateShape replaces the shape as one undoable step', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('rbox')
      result.current.addRBoxPoint([100, 100])
      result.current.addRBoxPoint([200, 100])
      result.current.addRBoxPoint([120, 140])
    })
    const drawn = lastValue(onChange).shapes[0]
    if (drawn.type !== 'rbox') throw new Error('expected an rbox')
    act(() => result.current.updateShape({ ...drawn, center: [300, 300] }))
    const moved = lastValue(onChange).shapes[0]
    if (moved.type === 'rbox') {
      expect(moved.center).toEqual([300, 300])
      expect(moved.size).toEqual([100, 40])
    }
    act(() => result.current.undo())
    expect(lastValue(onChange).shapes[0]).toEqual(drawn)
  })

  it('updateShape ignores an unknown id and an unchanged shape', () => {
    const { result, onChange } = setup()
    act(() => result.current.addPointShape([5, 5]))
    const shape = lastValue(onChange).shapes[0]
    onChange.mockClear()
    act(() => {
      result.current.updateShape(shape)
      result.current.updateShape({ ...shape, id: 'missing' })
    })
    expect(onChange).not.toHaveBeenCalled()
  })
})

describe('StrictMode double-invocation', () => {
  it('commits each drawn shape exactly once', () => {
    const onChange = vi.fn()
    const value = emptyResult()
    const { result } = renderHook(
      () => useDrawing({ value, onChange, classes, imageWidth: 1000, imageHeight: 800 }),
      { wrapper: StrictMode },
    )
    // One act per gesture, as separate pointer events would be.
    act(() => result.current.setTool('rbox'))
    act(() => result.current.addRBoxPoint([100, 100]))
    act(() => result.current.addRBoxPoint([200, 100]))
    act(() => result.current.addRBoxPoint([120, 140]))
    act(() => result.current.setTool('bbox'))
    act(() => result.current.beginBBoxDrag([10, 10]))
    act(() => result.current.updateBBoxDrag([50, 50]))
    act(() => result.current.commitBBoxDrag())
    act(() => result.current.setTool('polygon'))
    act(() => result.current.addPolygonPoint([0, 0]))
    act(() => result.current.addPolygonPoint([10, 0]))
    act(() => result.current.addPolygonPoint([10, 10]))
    act(() => result.current.closePolygon())
    expect(onChange).toHaveBeenCalledTimes(3)
    expect(lastValue(onChange).shapes.map((s) => s.type)).toEqual(['rbox', 'bbox', 'polygon'])
  })

  it('tells the parent about each undo and redo exactly once', () => {
    const onChange = vi.fn()
    const value = emptyResult()
    const { result } = renderHook(
      () => useDrawing({ value, onChange, classes, imageWidth: 1000, imageHeight: 800 }),
      { wrapper: StrictMode },
    )
    act(() => result.current.setTool('point'))
    act(() => result.current.addPointShape([5, 5]))
    act(() => result.current.addPointShape([6, 6]))
    onChange.mockClear()

    act(() => result.current.undo())
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(lastValue(onChange).shapes).toHaveLength(1)
    act(() => result.current.redo())
    expect(onChange).toHaveBeenCalledTimes(2)
    expect(lastValue(onChange).shapes).toHaveLength(2)
    // Nothing left to redo: no notification.
    act(() => result.current.redo())
    expect(onChange).toHaveBeenCalledTimes(2)
  })
})

describe('point tool', () => {
  it('commits a point shape on a single click', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('point')
      result.current.addPointShape([55, 12])
    })
    expect(onChange).toHaveBeenCalledTimes(1)
    const shape = lastValue(onChange).shapes[0]
    expect(shape.type).toBe('point')
    if (shape.type === 'point') expect(shape.point).toEqual([55, 12])
  })
})

describe('smart polygon tool (ML-7)', () => {
  it('a click without movement is a point prompt', () => {
    const { result } = setup()
    let prompt: ReturnType<typeof result.current.finishSmartDrag> = null
    act(() => {
      result.current.setTool('smart')
      result.current.beginSmartDrag([40, 30])
      result.current.updateSmartDrag([41, 31])
    })
    expect(result.current.draft?.tool).toBe('smart')
    act(() => {
      prompt = result.current.finishSmartDrag()
    })
    expect(prompt).toEqual({ kind: 'point', point: [40, 30] })
    expect(result.current.draft).toBeNull()
  })

  it('a drag is a normalised, clamped box prompt', () => {
    const { result } = setup()
    let prompt: ReturnType<typeof result.current.finishSmartDrag> = null
    act(() => {
      result.current.setTool('smart')
      result.current.beginSmartDrag([50, 40])
      result.current.updateSmartDrag([-10, 5])
    })
    act(() => {
      prompt = result.current.finishSmartDrag()
    })
    expect(prompt).toEqual({ kind: 'box', bbox: [0, 5, 50, 40] })
  })

  it('finishing without a smart draft returns null', () => {
    const { result } = setup()
    let prompt: ReturnType<typeof result.current.finishSmartDrag> = null
    act(() => {
      prompt = result.current.finishSmartDrag()
    })
    expect(prompt).toBeNull()
  })

  it('addPolygonShape commits the polygon in the active class, clamped and selected', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setActiveClassName('road')
    })
    act(() => {
      result.current.addPolygonShape([
        [-5, 10],
        [50, 10],
        [50, 9000],
      ])
    })
    expect(onChange).toHaveBeenCalledTimes(1)
    const shape = lastValue(onChange).shapes[0]
    expect(shape.type).toBe('polygon')
    expect(shape.class).toBe('road')
    if (shape.type === 'polygon') {
      expect(shape.points).toEqual([
        [0, 10],
        [50, 10],
        [50, 800],
      ])
    }
    expect(result.current.selectedId).toBe(shape.id)
    expect(result.current.canUndo).toBe(true)
  })

  it('addPolygonShape ignores fewer than three points and read-only mode', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.addPolygonShape([
        [1, 1],
        [2, 2],
      ])
    })
    expect(onChange).not.toHaveBeenCalled()

    const ro = setup(emptyResult(), true)
    act(() => {
      ro.result.current.addPolygonShape([
        [1, 1],
        [2, 1],
        [2, 2],
      ])
    })
    expect(ro.onChange).not.toHaveBeenCalled()
  })
})

describe('selection and delete', () => {
  function withOnePoint(): AnnotationResult {
    return {
      schema_version: 1,
      media_type: 'image',
      classification: {},
      shapes: [
        { id: 'x1', type: 'point', class: 'car', attributes: {}, confidence: null, point: [1, 1] },
      ],
    }
  }

  it('deletes the selected shape', () => {
    const { result, onChange } = setup(withOnePoint())
    act(() => {
      result.current.select('x1')
      result.current.deleteSelected()
    })
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(lastValue(onChange).shapes).toHaveLength(0)
    expect(result.current.selectedId).toBeNull()
  })

  it('does nothing when nothing is selected', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.deleteSelected()
    })
    expect(onChange).not.toHaveBeenCalled()
  })
})

describe('undo/redo integration', () => {
  it('undoes and redoes shape creation', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('point')
      result.current.addPointShape([1, 1])
    })
    expect(result.current.canUndo).toBe(true)
    expect(result.current.canRedo).toBe(false)

    act(() => {
      result.current.undo()
    })
    expect(lastValue(onChange).shapes).toHaveLength(0)
    expect(result.current.canUndo).toBe(false)
    expect(result.current.canRedo).toBe(true)

    act(() => {
      result.current.redo()
    })
    expect(lastValue(onChange).shapes).toHaveLength(1)
    expect(result.current.canRedo).toBe(false)
  })

  it('a fresh commit after undo drops the redo branch', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('point')
      result.current.addPointShape([1, 1])
    })
    act(() => {
      result.current.undo()
    })
    expect(result.current.canRedo).toBe(true)
    act(() => {
      result.current.addPointShape([2, 2])
    })
    expect(result.current.canRedo).toBe(false)
    expect(lastValue(onChange).shapes).toHaveLength(1)
  })

  it('pushes exactly one history entry per commit, not per intermediate drag update', () => {
    const { result, onChange } = setup()
    act(() => {
      result.current.setTool('bbox')
      result.current.beginBBoxDrag([0, 0])
      result.current.updateBBoxDrag([10, 10])
      result.current.updateBBoxDrag([20, 20])
      result.current.updateBBoxDrag([50, 50])
      result.current.commitBBoxDrag()
    })
    expect(onChange).toHaveBeenCalledTimes(1)
    expect(result.current.canUndo).toBe(true)
  })
})

describe('readOnly', () => {
  it('blocks drawing, moving and deleting but still allows selection', () => {
    const withShape: AnnotationResult = {
      schema_version: 1,
      media_type: 'image',
      classification: {},
      shapes: [
        { id: 'x1', type: 'point', class: 'car', attributes: {}, confidence: null, point: [1, 1] },
      ],
    }
    const { result, onChange } = setup(withShape, true)
    act(() => {
      result.current.select('x1')
      result.current.deleteSelected()
      result.current.addPointShape([5, 5])
      result.current.updateShape({ ...withShape.shapes[0], class: 'bus' })
      result.current.beginBBoxDrag([0, 0])
    })
    expect(onChange).not.toHaveBeenCalled()
    expect(result.current.selectedId).toBe('x1')
    expect(result.current.draft).toBeNull()
  })
})

describe('attributes (TOOL-2)', () => {
  const withAttributes: LabelClass[] = [
    {
      name: 'car',
      display_name: 'Car',
      color: '#e11d48',
      hotkey: '1',
      tools: ['bbox', 'point'],
      attributes: [
        { name: 'occluded', type: 'boolean', required: false, default: false },
        { name: 'make', type: 'select', required: false, options: ['Toyota', 'Volvo'] },
      ],
    },
    {
      name: 'sign',
      display_name: 'Sign',
      color: '#0ea5e9',
      hotkey: '2',
      tools: ['bbox', 'point'],
      attributes: [{ name: 'make', type: 'select', required: false, options: ['Toyota', 'Volvo'] }],
    },
  ]

  function setupWith(initial: AnnotationResult = emptyResult()) {
    const onChange = vi.fn()
    const { result, rerender } = renderHook(
      ({ value }: { value: AnnotationResult }) =>
        useDrawing({
          value,
          onChange,
          classes: withAttributes,
          imageWidth: 1000,
          imageHeight: 800,
        }),
      { initialProps: { value: initial } },
    )
    return { result, rerender, onChange }
  }

  it('defaultAttributes keeps only attributes that declare a default', () => {
    expect(defaultAttributes(withAttributes[0])).toEqual({ occluded: false })
    expect(defaultAttributes(withAttributes[1])).toEqual({})
    expect(defaultAttributes(undefined)).toEqual({})
  })

  it('reconcileAttributes drops values the new class does not define and adds its defaults', () => {
    expect(reconcileAttributes({ make: 'Volvo', occluded: true }, withAttributes[1])).toEqual({
      make: 'Volvo',
    })
    expect(reconcileAttributes({ make: 'Volvo' }, withAttributes[0])).toEqual({
      occluded: false,
      make: 'Volvo',
    })
  })

  it('a new shape starts with the active class defaults', () => {
    const { result, onChange } = setupWith()
    act(() => {
      result.current.setTool('point')
      result.current.addPointShape([5, 5])
    })
    expect(lastValue(onChange).shapes[0].attributes).toEqual({ occluded: false })

    act(() => {
      result.current.setTool('bbox')
      result.current.beginBBoxDrag([10, 10])
      result.current.updateBBoxDrag([100, 100])
      result.current.commitBBoxDrag()
    })
    expect(lastValue(onChange).shapes[1].attributes).toEqual({ occluded: false })
  })

  it('re-classifying by hotkey reconciles the attributes to the new class', () => {
    const { result, onChange } = setupWith()
    act(() => {
      result.current.setTool('point')
      result.current.addPointShape([5, 5])
    })
    act(() => {
      result.current.applyHotkeyClass('2')
    })
    const shape = lastValue(onChange).shapes[0]
    expect(shape.class).toBe('sign')
    expect(shape.attributes).toEqual({})
  })

  it('an external edit of `value` is one undoable step, not a history reset', () => {
    const initial = emptyResult()
    const { result, rerender, onChange } = setupWith(initial)
    act(() => {
      result.current.setTool('point')
      result.current.addPointShape([5, 5])
    })
    const drawn = lastValue(onChange)
    // The parent round-trips our commit first (same reference: no-op)…
    rerender({ value: drawn })
    expect(result.current.canUndo).toBe(true)

    // …then edits an attribute in its sidebar.
    const edited: AnnotationResult = {
      ...drawn,
      shapes: [{ ...drawn.shapes[0], attributes: { occluded: true } }],
    }
    rerender({ value: edited })

    act(() => {
      result.current.undo()
    })
    expect(lastValue(onChange)).toBe(drawn)
    act(() => {
      result.current.undo()
    })
    expect(lastValue(onChange)).toBe(initial)
    expect(result.current.canUndo).toBe(false)
  })
})

describe('brush and eraser (masks)', () => {
  function smallSetup(initial: AnnotationResult = emptyResult()) {
    const onChange = vi.fn()
    const { result } = renderHook(() =>
      useDrawing({ value: initial, onChange, classes, imageWidth: 20, imageHeight: 10 }),
    )
    return { result, onChange }
  }

  it('paints a new mask in the active class and selects it', () => {
    const { result, onChange } = smallSetup()
    act(() => result.current.beginStroke([5, 5], 2, false))
    act(() => result.current.extendStroke([15, 5]))
    act(() => result.current.commitStroke())
    const shapes = lastValue(onChange).shapes
    expect(shapes).toHaveLength(1)
    const mask = shapes[0]
    if (mask.type !== 'mask') throw new Error('expected a mask')
    expect(mask.class).toBe('car')
    expect(mask.rle.size).toEqual([10, 20])
    expect(mask.rle.counts.reduce((a, b) => a + b, 0)).toBe(200)
    expect(result.current.selectedId).toBe(mask.id)
    expect(result.current.draft).toBeNull()
  })

  it('grows the selected mask, then erases it away as one undo step each', () => {
    const { result, onChange } = smallSetup()
    act(() => result.current.beginStroke([5, 5], 2, false))
    act(() => result.current.commitStroke())
    const first = lastValue(onChange).shapes[0]
    act(() => result.current.beginStroke([15, 5], 2, false))
    act(() => result.current.commitStroke())
    const grown = lastValue(onChange).shapes
    expect(grown).toHaveLength(1)
    expect(grown[0].id).toBe(first.id)
    if (grown[0].type !== 'mask' || first.type !== 'mask') throw new Error('expected masks')
    expect(grown[0].rle.counts.length).toBeGreaterThan(first.rle.counts.length)

    act(() => result.current.beginStroke([10, 5], 20, true))
    act(() => result.current.commitStroke())
    expect(lastValue(onChange).shapes).toHaveLength(0)
    expect(result.current.selectedId).toBeNull()

    act(() => result.current.undo())
    expect(lastValue(onChange).shapes[0]).toEqual(grown[0])
  })

  it('erases nothing when no mask is selected', () => {
    const { result, onChange } = smallSetup()
    act(() => result.current.beginStroke([5, 5], 2, true))
    act(() => result.current.commitStroke())
    expect(onChange).not.toHaveBeenCalled()
  })

  it('does not paint in read-only mode', () => {
    const onChange = vi.fn()
    const { result } = renderHook(() =>
      useDrawing({
        value: emptyResult(),
        onChange,
        classes,
        imageWidth: 20,
        imageHeight: 10,
        readOnly: true,
      }),
    )
    act(() => result.current.beginStroke([5, 5], 2, false))
    expect(result.current.draft).toBeNull()
  })
})

describe('keypoint skeletons', () => {
  const skeletonClasses: LabelClass[] = [
    ...classes,
    {
      name: 'person',
      display_name: 'Person',
      color: '#0ea5e9',
      hotkey: '3',
      tools: ['keypoints'],
      attributes: [],
      skeleton: { points: ['head', 'left', 'right'], edges: [[0, 1], [0, 2]] },
    },
  ]

  function kpSetup() {
    const onChange = vi.fn()
    const initial = emptyResult()
    const { result } = renderHook(() =>
      useDrawing({
        value: initial,
        onChange,
        classes: skeletonClasses,
        imageWidth: 1000,
        imageHeight: 800,
      }),
    )
    return { result, onChange }
  }

  it('starts in the first skeleton class and commits on the last point', () => {
    const { result, onChange } = kpSetup()
    expect(result.current.keypointsClass?.name).toBe('person')
    act(() => result.current.addKeypoint([10, 20], false))
    act(() => result.current.addKeypoint([30, 40], true))
    expect(onChange).not.toHaveBeenCalled()
    expect(result.current.draft).toMatchObject({ tool: 'keypoints', className: 'person' })
    act(() => result.current.skipKeypoint())
    const [shape] = lastValue(onChange).shapes
    expect(shape).toMatchObject({
      type: 'keypoints',
      class: 'person',
      points: [
        [10, 20, 2],
        [30, 40, 1],
        [0, 0, 0],
      ],
    })
    expect(result.current.selectedId).toBe(shape.id)
    expect(result.current.draft).toBeNull()
  })

  it('finishes early with the rest unlabelled, and drops the last point on undo-point', () => {
    const { result, onChange } = kpSetup()
    act(() => result.current.addKeypoint([10, 20], false))
    act(() => result.current.addKeypoint([30, 40], false))
    act(() => result.current.removeLastPolygonPoint())
    act(() => result.current.finishKeypoints())
    const [shape] = lastValue(onChange).shapes
    if (shape.type !== 'keypoints') throw new Error('expected keypoints')
    expect(shape.points).toEqual([
      [10, 20, 2],
      [0, 0, 0],
      [0, 0, 0],
    ])
  })

  it('commits nothing when every point was skipped', () => {
    const { result, onChange } = kpSetup()
    act(() => result.current.skipKeypoint())
    act(() => result.current.finishKeypoints())
    expect(onChange).not.toHaveBeenCalled()
    expect(result.current.draft).toBeNull()
  })

  it('uses the active class when it has a skeleton, and clamps to the image', () => {
    const { result, onChange } = kpSetup()
    act(() => result.current.setActiveClassName('person'))
    act(() => result.current.addKeypoint([-5, 900], false))
    act(() => result.current.finishKeypoints())
    const [shape] = lastValue(onChange).shapes
    if (shape.type !== 'keypoints') throw new Error('expected keypoints')
    expect(shape.points[0]).toEqual([0, 800, 2])
  })

  it('does nothing without a skeleton class', () => {
    const { result, onChange } = setup()
    expect(result.current.keypointsClass).toBeNull()
    act(() => result.current.addKeypoint([10, 20], false))
    expect(result.current.draft).toBeNull()
    expect(onChange).not.toHaveBeenCalled()
  })
})
