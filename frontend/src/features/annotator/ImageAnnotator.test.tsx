/**
 * The annotator's media states (IMG-3).
 *
 * A failed image load used to look exactly like a slow one: a blank canvas
 * with the drawing tools live, so an annotator could draw boxes onto nothing
 * and never learn why. These tests pin the visible difference.
 *
 * Konva draws to a real canvas, which jsdom has no use for, so `react-konva`
 * is replaced by plain elements. Nothing here asserts on the canvas itself —
 * only on the messages rendered beside it.
 */

import { fireEvent, render, screen, within } from '@testing-library/react'
import { act, useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ImageAnnotator } from './ImageAnnotator'
import type { SelectRequest } from './ImageAnnotator'
import { useImageAdjustStore } from './ImageAdjust'
import { NEUTRAL } from './adjustment'
import type { AnnotationResult, LabelClass } from './types'

vi.mock('react-konva', async () => {
  const React = await import('react')
  const passthrough =
    (tag: string) =>
    ({ children }: { children?: React.ReactNode }) =>
      React.createElement(tag === 'stage' ? 'div' : 'span', { 'data-konva': tag }, children)
  return {
    Stage: passthrough('stage'),
    Layer: passthrough('layer'),
    Image: passthrough('image'),
    Line: passthrough('line'),
    Rect: passthrough('rect'),
    Circle: passthrough('circle'),
    Group: passthrough('group'),
    Text: passthrough('text'),
  }
})

/** jsdom has no layout engine, so the canvas container never resizes. The
annotator only needs the observer to exist; a zero-size stage is fine here. */
class NoopResizeObserver {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

/** The `Image` instances the component created, newest last. */
const created: MockImage[] = []

class MockImage {
  crossOrigin = ''
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  private _src = ''

  constructor() {
    created.push(this)
  }

  set src(value: string) {
    this._src = value
  }

  get src(): string {
    return this._src
  }
}

const CLASSES: LabelClass[] = [
  { name: 'cat', display_name: 'Cat', color: '#ff0000', tools: ['bbox', 'mask'], attributes: [] },
]

const EMPTY: AnnotationResult = {
  schema_version: 1,
  media_type: 'image',
  classification: {},
  shapes: [],
}

function renderAnnotator(imageUrl = 'https://storage.example/cat.jpg?sig=abc') {
  return render(
    <ImageAnnotator
      imageUrl={imageUrl}
      imageWidth={800}
      imageHeight={600}
      classes={CLASSES}
      value={EMPTY}
      onChange={() => {}}
    />,
  )
}

describe('ImageAnnotator media states', () => {
  beforeEach(() => {
    created.length = 0
    vi.stubGlobal('Image', MockImage)
    vi.stubGlobal('ResizeObserver', NoopResizeObserver)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('says it is loading until the image arrives', () => {
    renderAnnotator()

    expect(screen.getByRole('status')).toHaveTextContent('Loading image…')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('explains a failed load instead of leaving the canvas blank', () => {
    renderAnnotator()

    act(() => {
      created[created.length - 1].onerror?.()
    })

    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('This image could not be loaded.')
    expect(alert).toHaveTextContent(/expired/)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('clears both messages once the image loads', () => {
    renderAnnotator()

    act(() => {
      created[created.length - 1].onload?.()
    })

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('requests the media without credentials', () => {
    renderAnnotator()

    expect(created[created.length - 1].crossOrigin).toBe('anonymous')
  })
})

describe('ImageAnnotator selection (TOOL-2)', () => {
  beforeEach(() => {
    vi.stubGlobal('Image', MockImage)
    vi.stubGlobal('ResizeObserver', NoopResizeObserver)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('reports the (empty) selection to the page on mount', () => {
    const onSelectionChange = vi.fn()
    render(
      <ImageAnnotator
        imageUrl="https://storage.example/cat.jpg?sig=abc"
        imageWidth={800}
        imageHeight={600}
        classes={CLASSES}
        value={EMPTY}
        onChange={() => {}}
        onSelectionChange={onSelectionChange}
      />,
    )
    expect(onSelectionChange).toHaveBeenCalledWith(null)
  })

  it('offers the rotated box and polyline tools, with hotkeys R and L', () => {
    renderAnnotator()
    const toolbar = screen.getByRole('toolbar', { name: 'Annotation tools' })
    const rbox = within(toolbar).getByRole('button', { name: /Rotated box/ })
    const polyline = within(toolbar).getByRole('button', { name: /Polyline/ })
    expect(rbox).toHaveAttribute('aria-pressed', 'false')

    fireEvent.keyDown(window, { key: 'r' })
    expect(rbox).toHaveAttribute('aria-pressed', 'true')

    fireEvent.keyDown(window, { key: 'l' })
    expect(polyline).toHaveAttribute('aria-pressed', 'true')
    expect(rbox).toHaveAttribute('aria-pressed', 'false')
  })

  it('lights the hand while Space is held, then returns to the tool', () => {
    renderAnnotator()
    const toolbar = screen.getByRole('toolbar', { name: 'Annotation tools' })
    const hand = within(toolbar).getByRole('button', { name: /^Hand/ })
    const box = within(toolbar).getByRole('button', { name: /^Box/ })
    fireEvent.keyDown(window, { key: 'b' })
    expect(box).toHaveClass('bg-accent-fill')

    fireEvent.keyDown(window, { key: ' ', code: 'Space' })
    expect(hand).toHaveClass('bg-accent-fill')
    expect(box).not.toHaveClass('bg-accent-fill')
    // Still the tool in effect, so it stays pressed for assistive tech.
    expect(box).toHaveAttribute('aria-pressed', 'true')

    fireEvent.keyUp(window, { key: ' ', code: 'Space' })
    expect(box).toHaveClass('bg-accent-fill')
    expect(hand).not.toHaveClass('bg-accent-fill')
  })

  it('deletes the selected shape with Delete or Backspace', () => {
    function Harness() {
      const [value, setValue] = useState<AnnotationResult>({
        ...EMPTY,
        shapes: [
          { id: 'a', type: 'bbox', class: 'cat', bbox: [10, 10, 50, 50] },
          { id: 'b', type: 'bbox', class: 'cat', bbox: [60, 60, 90, 90] },
        ],
      } as AnnotationResult)
      return (
        <>
          <ImageAnnotator
            imageUrl="https://storage.example/cat.jpg?sig=abc"
            imageWidth={800}
            imageHeight={600}
            classes={CLASSES}
            value={value}
            onChange={setValue}
            selectRequest={{ id: 'a', seq: 1 }}
          />
          <output data-testid="count">{value.shapes.length}</output>
        </>
      )
    }
    render(<Harness />)
    expect(screen.getByTestId('count')).toHaveTextContent('2')
    fireEvent.keyDown(window, { key: 'Backspace' })
    expect(screen.getByTestId('count')).toHaveTextContent('1')
  })

  it('switches to the hand tool with H', () => {
    renderAnnotator()
    const toolbar = screen.getByRole('toolbar', { name: 'Annotation tools' })
    fireEvent.keyDown(window, { key: 'h' })
    expect(within(toolbar).getByRole('button', { name: /^Hand/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })
})

describe('ImageAnnotator mask brush', () => {
  beforeEach(() => {
    vi.stubGlobal('Image', MockImage)
    vi.stubGlobal('ResizeObserver', NoopResizeObserver)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('offers brush (K) and eraser (E) with a size control that [ and ] change', () => {
    renderAnnotator()
    const toolbar = screen.getByRole('toolbar', { name: 'Annotation tools' })
    const brush = within(toolbar).getByRole('button', { name: /^Brush/ })
    expect(screen.queryByRole('slider', { name: 'Brush size' })).toBeNull()

    fireEvent.keyDown(window, { key: 'k' })
    expect(brush).toHaveAttribute('aria-pressed', 'true')
    const size = screen.getByRole('slider', { name: 'Brush size' })
    expect(size).toHaveValue('24')
    fireEvent.keyDown(window, { key: ']' })
    expect(size).toHaveValue('30')
    fireEvent.keyDown(window, { key: '[' })
    expect(size).toHaveValue('24')

    fireEvent.keyDown(window, { key: 'e' })
    expect(within(toolbar).getByRole('button', { name: /^Eraser/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('offers superpixels (X) with a detail control, and explains an unreadable image', async () => {
    renderAnnotator()
    const toolbar = screen.getByRole('toolbar', { name: 'Annotation tools' })
    const tool = within(toolbar).getByRole('button', { name: /^Superpixels/ })
    expect(screen.queryByRole('slider', { name: 'Superpixel detail' })).toBeNull()

    fireEvent.keyDown(window, { key: 'x' })
    expect(tool).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('slider', { name: 'Superpixel detail' })).toHaveValue('1200')

    // The pixels are read once the image is in; jsdom has no 2d canvas, which
    // stands in for a storage that does not allow CORS.
    const getContext = vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null)
    act(() => created[created.length - 1].onload?.())
    expect(await screen.findByRole('alert')).toHaveTextContent(/CORS/)
    getContext.mockRestore()
  })

  it('is not offered when no class allows masks', () => {
    render(
      <ImageAnnotator
        imageUrl="https://storage.example/cat.jpg?sig=abc"
        imageWidth={800}
        imageHeight={600}
        classes={[{ ...CLASSES[0], tools: ['bbox'] }]}
        value={EMPTY}
        onChange={() => {}}
      />,
    )
    expect(screen.queryByRole('button', { name: /^Brush/ })).toBeNull()
    fireEvent.keyDown(window, { key: 'k' })
    expect(screen.queryByRole('slider', { name: 'Brush size' })).toBeNull()
  })

  it('is not offered read-only', () => {
    render(
      <ImageAnnotator
        imageUrl="https://storage.example/cat.jpg?sig=abc"
        imageWidth={800}
        imageHeight={600}
        classes={CLASSES}
        value={EMPTY}
        onChange={() => {}}
        readOnly
      />,
    )
    const toolbar = screen.getByRole('toolbar', { name: 'Annotation tools' })
    expect(within(toolbar).queryByRole('button', { name: /^Brush/ })).toBeNull()
  })
})

describe('ImageAnnotator keypoints', () => {
  beforeEach(() => {
    vi.stubGlobal('Image', MockImage)
    vi.stubGlobal('ResizeObserver', NoopResizeObserver)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('is offered with J only when a class has a skeleton, and prompts for the next point', () => {
    render(
      <ImageAnnotator
        imageUrl="https://storage.example/cat.jpg?sig=abc"
        imageWidth={800}
        imageHeight={600}
        classes={[
          ...CLASSES,
          {
            name: 'person',
            display_name: 'Person',
            color: '#00ff00',
            tools: ['keypoints'],
            attributes: [],
            skeleton: { points: ['head', 'tail'], edges: [[0, 1]] },
          },
        ]}
        value={EMPTY}
        onChange={() => {}}
      />,
    )
    const keypoints = screen.getByRole('button', { name: /^Keypoints/ })
    fireEvent.keyDown(window, { key: 'j' })
    expect(keypoints).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText(/Person: place/)).toHaveTextContent('Person: place head (1/2)')
  })

  it('is not offered without a skeleton class', () => {
    renderAnnotator()
    expect(screen.queryByRole('button', { name: /^Keypoints/ })).toBeNull()
  })
})

describe('ImageAnnotator smart polygon (ML-7)', () => {
  beforeEach(() => {
    vi.stubGlobal('Image', MockImage)
    vi.stubGlobal('ResizeObserver', NoopResizeObserver)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('is not offered, and S does nothing, without a segment model', () => {
    renderAnnotator()
    const toolbar = screen.getByRole('toolbar', { name: 'Annotation tools' })
    expect(within(toolbar).queryByRole('button', { name: /Smart polygon/ })).toBeNull()

    fireEvent.keyDown(window, { key: 'b' })
    fireEvent.keyDown(window, { key: 's' })
    expect(within(toolbar).getByRole('button', { name: /^Box/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
  })

  it('is offered with hotkey S when the page can ask a model', () => {
    render(
      <ImageAnnotator
        imageUrl="https://storage.example/cat.jpg?sig=abc"
        imageWidth={800}
        imageHeight={600}
        classes={CLASSES}
        value={EMPTY}
        onChange={() => {}}
        onSmartPrompt={() => Promise.resolve(null)}
      />,
    )
    const toolbar = screen.getByRole('toolbar', { name: 'Annotation tools' })
    const smart = within(toolbar).getByRole('button', { name: /Smart polygon/ })
    expect(smart).toHaveAttribute('aria-pressed', 'false')

    fireEvent.keyDown(window, { key: 's' })
    expect(smart).toHaveAttribute('aria-pressed', 'true')
  })

  describe('image adjustments (IMG-5)', () => {
    afterEach(() => useImageAdjustStore.setState({ adjustment: NEUTRAL }))

    it('opens from the toolbar or with I, and drives the SVG filter', () => {
      const { container } = renderAnnotator()
      expect(screen.queryByRole('group', { name: 'Image adjustments' })).not.toBeInTheDocument()

      fireEvent.click(screen.getByRole('button', { name: 'Image' }))
      const panel = screen.getByRole('group', { name: 'Image adjustments' })
      fireEvent.change(within(panel).getByLabelText('W'), { target: { value: '51' } })
      fireEvent.change(within(panel).getByLabelText('L'), { target: { value: '51' } })
      fireEvent.change(within(panel).getByLabelText('Channel'), { target: { value: 'gray' } })

      expect(useImageAdjustStore.getState().adjustment).toEqual({
        window: 51,
        level: 51,
        channel: 'gray',
      })
      const funcR = container.querySelector('feFuncR')
      expect(Number(funcR?.getAttribute('slope'))).toBeCloseTo(5)
      expect(Number(funcR?.getAttribute('intercept'))).toBeCloseTo(-0.5)

      fireEvent.click(within(panel).getByRole('button', { name: 'Reset' }))
      expect(useImageAdjustStore.getState().adjustment).toEqual(NEUTRAL)

      fireEvent.keyDown(window, { key: 'i' })
      expect(screen.queryByRole('group', { name: 'Image adjustments' })).not.toBeInTheDocument()
    })
  })
})

describe('ImageAnnotator without a pointer (UX-7, WCAG 2.1.1)', () => {
  beforeEach(() => {
    vi.stubGlobal('Image', MockImage)
    vi.stubGlobal('ResizeObserver', NoopResizeObserver)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  const KEYBOARD_CLASSES: LabelClass[] = [
    { name: 'cat', display_name: 'Cat', color: '#ff0000', tools: ['bbox', 'point'], attributes: [] },
  ]

  /** Controlled like the page does it, so each committed shape is kept. */
  function Harness({
    onChange,
    readOnly = false,
    selectRequest,
    onSelectionChange,
    initial = EMPTY,
  }: {
    onChange: (next: AnnotationResult) => void
    readOnly?: boolean
    selectRequest?: SelectRequest | null
    onSelectionChange?: (id: string | null) => void
    initial?: AnnotationResult
  }) {
    const [value, setValue] = useState(initial)
    return (
      <ImageAnnotator
        imageUrl="https://storage.example/cat.jpg?sig=abc"
        imageWidth={800}
        imageHeight={600}
        classes={KEYBOARD_CLASSES}
        value={value}
        readOnly={readOnly}
        selectRequest={selectRequest}
        onSelectionChange={onSelectionChange}
        onChange={(next) => {
          setValue(next)
          onChange(next)
        }}
      />
    )
  }

  function press(key: string, init: Partial<KeyboardEventInit> = {}): void {
    fireEvent.keyDown(window, { key, code: key === ' ' ? 'Space' : key, ...init })
    if (key === ' ') fireEvent.keyUp(window, { key, code: 'Space' })
  }

  function cursorStatus(): HTMLElement {
    return screen.getByText(/^Keyboard cursor at/)
  }

  it('draws a box with the arrows and two presses of Space', () => {
    const onChange = vi.fn()
    render(<Harness onChange={onChange} />)
    act(() => created[created.length - 1].onload?.())

    press('b')
    press('ArrowRight') // shows the crosshair where it starts
    const [x0, y0] = (cursorStatus().textContent?.match(/(\d+), (\d+)/) ?? [])
      .slice(1)
      .map(Number)
    press('ArrowRight')
    press('ArrowRight')
    expect(cursorStatus()).toHaveTextContent(`${x0 + 20}, ${y0}`)

    press(' ') // first corner
    press('ArrowRight')
    press('ArrowRight')
    press('ArrowRight', { shiftKey: true })
    press('ArrowDown')
    press('ArrowDown')
    press(' ') // opposite corner

    const shapes = (onChange.mock.lastCall?.[0] as AnnotationResult).shapes
    expect(shapes).toHaveLength(1)
    expect(shapes[0]).toMatchObject({ type: 'bbox', class: 'cat' })
    // [xMin, yMin, xMax, yMax]: from the first press to where the arrows went.
    expect((shapes[0] as { bbox: number[] }).bbox).toEqual([x0 + 20, y0, x0 + 90, y0 + 20])
  })

  it('places a point, and Escape puts the crosshair away', () => {
    const onChange = vi.fn()
    render(<Harness onChange={onChange} />)
    act(() => created[created.length - 1].onload?.())

    press('p')
    press('ArrowDown')
    press('ArrowDown')
    press(' ')
    expect((onChange.mock.lastCall?.[0] as AnnotationResult).shapes).toEqual([
      expect.objectContaining({ type: 'point', class: 'cat' }),
    ])

    press('Escape')
    expect(screen.queryByText(/^Keyboard cursor at/)).not.toBeInTheDocument()
    press(' ')
    expect((onChange.mock.lastCall?.[0] as AnnotationResult).shapes).toHaveLength(1)
  })

  it('consumes a tool key so the page does not also act on it, but not read-only', () => {
    const { unmount } = render(<Harness onChange={() => {}} />)
    // fireEvent returns false when a listener called preventDefault.
    expect(fireEvent.keyDown(window, { key: 'p' })).toBe(false)
    expect(fireEvent.keyDown(window, { key: 'q' })).toBe(true)
    // Shift+P is the page's "previous item", even on an image.
    expect(fireEvent.keyDown(window, { key: 'P', shiftKey: true })).toBe(true)
    unmount()

    render(<Harness onChange={() => {}} readOnly />)
    expect(fireEvent.keyDown(window, { key: 'r' })).toBe(true)
  })

  it('shows no crosshair read-only', () => {
    render(<Harness onChange={() => {}} readOnly />)
    press('b')
    press('ArrowRight')
    expect(screen.queryByText(/^Keyboard cursor at/)).not.toBeInTheDocument()
  })

  it('selects the shape the page asks for', () => {
    const onSelectionChange = vi.fn()
    const withBox: AnnotationResult = {
      ...EMPTY,
      shapes: [
        {
          id: 's1',
          type: 'bbox',
          class: 'cat',
          bbox: [1, 2, 30, 40],
          attributes: {},
          confidence: null,
        },
      ],
    }
    const { rerender } = render(
      <Harness onChange={() => {}} initial={withBox} onSelectionChange={onSelectionChange} />,
    )
    expect(onSelectionChange).toHaveBeenLastCalledWith(null)

    rerender(
      <Harness
        onChange={() => {}}
        initial={withBox}
        selectRequest={{ id: 's1', seq: 1 }}
        onSelectionChange={onSelectionChange}
      />,
    )
    expect(onSelectionChange).toHaveBeenLastCalledWith('s1')

    // The same request again is not a new one; a new sequence number is.
    rerender(
      <Harness
        onChange={() => {}}
        initial={withBox}
        selectRequest={{ id: null, seq: 2 }}
        onSelectionChange={onSelectionChange}
      />,
    )
    expect(onSelectionChange).toHaveBeenLastCalledWith(null)
  })
})
