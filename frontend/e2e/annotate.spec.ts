/**
 * The annotator on a real item: media served straight from storage through a
 * signed URL (ARC-3), a box drawn on the Konva canvas, undo, and a draft saved
 * as an annotation version. Runs in browse mode (direct item URL) so no task
 * lock is taken and the item's status is left alone.
 */
import type { APIRequestContext, Locator, Page } from '@playwright/test'

import {
  annotatorCanvas,
  clickPoints,
  createScratchProject,
  dragBox,
  expect,
  test as base,
  type DemoProject,
} from './support'

const SAMPLE_GLOB = '*/scene-003-*.jpg'

// The annotator opens an item on its latest version, so a draft saved on a
// shared item would be on every later run's canvas (and under its drags).
// Each test gets an item of its own: a scratch project with one sample,
// hard-deleted afterwards. `demo` keeps its shape so the tests read the same.
const test = base.extend<{ demo: DemoProject }>({
  demo: async ({ api }, use, testInfo) => {
    const project = await createScratchProject(
      api,
      `E2E annotate ${testInfo.workerIndex}`,
      SAMPLE_GLOB,
    )
    try {
      expect(project.itemIds, `glob ${SAMPLE_GLOB} should match exactly one sample`).toHaveLength(1)
      await use({ id: project.id, name: 'E2E annotate', firstItemId: project.itemIds[0] })
    } finally {
      await api.delete(`/api/v1/projects/${project.id}`)
    }
  },
})

/** Number in the sidebar's "Annotations (n)" heading. */
async function shapeCount(page: Page): Promise<number> {
  const heading = page.getByRole('heading', { name: /^Annotations \(\d+\)$/ })
  const text = await heading.textContent()
  return Number(/\((\d+)\)/.exec(text ?? '')?.[1] ?? Number.NaN)
}

async function expectShapeCount(page: Page, count: number): Promise<void> {
  await expect(page.getByRole('heading', { name: `Annotations (${count})` })).toBeVisible()
}

/** Waits for web fonts and for the canvas to stop resizing: on a cold load the
 * toolbar can still rewrap, and the image is refitted to the new canvas. */
async function settledCanvas(page: Page, canvas: Locator): Promise<void> {
  await page.evaluate(() => document.fonts.ready.then(() => undefined))
  let previous = ''
  await expect
    .poll(
      async () => {
        const now = JSON.stringify(await canvas.boundingBox())
        const settled = now === previous
        previous = now
        return settled
      },
      { intervals: [250] },
    )
    .toBe(true)
}

/** The demo schema has no mask class; allow masks on its first class. */
async function allowMasks(api: APIRequestContext, projectId: string): Promise<void> {
  const schemas = (await (await api.get(`/api/v1/projects/${projectId}/schemas`)).json()) as Array<{
    version: number
    definition: { classes: Array<{ tools: string[] }> }
  }>
  const latest = schemas.reduce((a, b) => (b.version > a.version ? b : a))
  const [first, ...rest] = latest.definition.classes
  const schema = await api.post(`/api/v1/projects/${projectId}/schemas`, {
    data: {
      ...latest.definition,
      version: latest.version + 1,
      classes: [{ ...first, tools: [...first.tools, 'mask'] }, ...rest],
    },
  })
  expect(schema.status(), await schema.text()).toBe(201)
}

async function openItem(page: Page, projectId: string, itemId: string): Promise<Locator> {
  await page.goto(`/projects/${projectId}/annotate/${itemId}`)
  return annotatorCanvas(page)
}

test('the demo item loads with its label schema', async ({ page, demo }) => {
  await openItem(page, demo.id, demo.firstItemId)

  await expect(page.getByRole('heading', { name: 'Classes' })).toBeVisible()
  await expect(page.getByRole('heading', { name: /^Annotations \(\d+\)$/ })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Submit' })).toBeEnabled()
})

test('drawing a box, undoing it and redoing it updates the shape list', async ({ page, demo }) => {
  const canvas = await openItem(page, demo.id, demo.firstItemId)
  const initial = await shapeCount(page)

  await page.getByRole('button', { name: /^Box/ }).click()
  await expect(page.getByRole('button', { name: /^Box/ })).toHaveAttribute('aria-pressed', 'true')

  await dragBox(page, canvas, [0.3, 0.3], [0.6, 0.6])
  await expectShapeCount(page, initial + 1)

  await page.getByRole('button', { name: 'Undo' }).click()
  await expectShapeCount(page, initial)

  await page.getByRole('button', { name: 'Redo' }).click()
  await expectShapeCount(page, initial + 1)
})

test('the polyline tool commits an open path on Enter', async ({ page, demo }) => {
  const canvas = await openItem(page, demo.id, demo.firstItemId)
  const initial = await shapeCount(page)

  await page.keyboard.press('l')
  await expect(page.getByRole('button', { name: /^Polyline/ })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
  await clickPoints(page, canvas, [
    [0.2, 0.2],
    [0.4, 0.5],
    [0.6, 0.3],
  ])
  await expectShapeCount(page, initial)
  await page.keyboard.press('Enter')
  await expectShapeCount(page, initial + 1)

  await page.getByRole('button', { name: 'Undo' }).click()
  await expectShapeCount(page, initial)
})

test('the rotated box tool commits on the third click', async ({ page, demo }) => {
  const canvas = await openItem(page, demo.id, demo.firstItemId)
  const initial = await shapeCount(page)

  await page.keyboard.press('r')
  await expect(page.getByRole('button', { name: /^Rotated box/ })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
  await clickPoints(page, canvas, [
    [0.3, 0.3],
    [0.6, 0.4],
  ])
  await expectShapeCount(page, initial)
  await clickPoints(page, canvas, [[0.5, 0.6]])
  await expectShapeCount(page, initial + 1)

  await page.getByRole('button', { name: 'Undo' }).click()
  await expectShapeCount(page, initial)
})

test('the smart polygon tool turns a dragged box into a model-drawn polygon (ML-7)', async ({
  page,
  demo,
}) => {
  // The seed registers the compose model service as a segment model; its
  // heuristic `/interactive` answers a polygon for any box, so this exercises
  // the whole path: canvas gesture → API proxy → model → shape on the canvas.
  const canvas = await openItem(page, demo.id, demo.firstItemId)
  const initial = await shapeCount(page)
  await expect(page.getByRole('heading', { name: 'Smart polygon' })).toBeVisible()

  await page.getByRole('button', { name: /^Smart polygon/ }).click()
  await expect(page.getByRole('button', { name: /^Smart polygon/ })).toHaveAttribute(
    'aria-pressed',
    'true',
  )

  await dragBox(page, canvas, [0.3, 0.3], [0.6, 0.6])
  await expectShapeCount(page, initial + 1)
  await expect(page.getByRole('alert')).toHaveCount(0)

  await page.getByRole('button', { name: 'Undo' }).click()
  await expectShapeCount(page, initial)
})

test('saving a draft persists an annotation version with the drawn box', async ({
  page,
  api,
  demo,
}) => {
  const canvas = await openItem(page, demo.id, demo.firstItemId)
  const before = await api.get(`/api/v1/items/${demo.firstItemId}/annotations`)
  expect(before.ok()).toBeTruthy()
  const countBefore = ((await before.json()) as unknown[]).length

  const initial = await shapeCount(page)
  await page.keyboard.press('b')
  await dragBox(page, canvas, [0.2, 0.2], [0.5, 0.7])
  await expectShapeCount(page, initial + 1)

  const saved = page.waitForResponse(
    (response) =>
      response.url().includes(`/items/${demo.firstItemId}/annotations`) &&
      response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: 'Save draft' }).click()
  expect((await saved).ok()).toBeTruthy()
  await expect(page.getByRole('button', { name: 'Save draft' })).toBeEnabled()

  const after = await api.get(`/api/v1/items/${demo.firstItemId}/annotations`)
  const versions = (await after.json()) as Array<{
    status: string
    source: string
    result: { shapes: Array<{ type: string }> }
  }>
  expect(versions.length).toBe(countBefore + 1)
  const ours = versions.find(
    (version) =>
      version.status === 'draft' &&
      version.source === 'human' &&
      version.result.shapes.some((shape) => shape.type === 'bbox'),
  )
  expect(ours).toBeTruthy()
})

test('the brush paints a mask saved as COCO RLE, and the eraser removes it', async ({
  page,
  api,
  demo,
}) => {
  await allowMasks(api, demo.id)
  const canvas = await openItem(page, demo.id, demo.firstItemId)
  const initial = await shapeCount(page)

  await page.keyboard.press('k')
  await expect(page.getByRole('button', { name: /^Brush/ })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
  await dragBox(page, canvas, [0.4, 0.4], [0.6, 0.5])
  await expectShapeCount(page, initial + 1)
  // A second stroke grows the selected mask rather than adding a shape.
  await dragBox(page, canvas, [0.4, 0.6], [0.6, 0.6])
  await expectShapeCount(page, initial + 1)

  const saved = page.waitForResponse(
    (response) =>
      response.url().includes(`/items/${demo.firstItemId}/annotations`) &&
      response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: 'Save draft' }).click()
  const body = (await (await saved).json()) as {
    result: { shapes: Array<{ type: string; rle?: { size: [number, number]; counts: number[] } }> }
  }
  const masks = body.result.shapes.filter((shape) => shape.type === 'mask')
  expect(masks).toHaveLength(1)
  const { size, counts } = masks[0].rle!
  expect(counts.reduce((a, b) => a + b, 0)).toBe(size[0] * size[1])
  expect(counts.length).toBeGreaterThan(2)

  await page.keyboard.press('e')
  await page.getByRole('slider', { name: 'Brush size' }).fill('200')
  for (const y of [0.4, 0.45, 0.5, 0.55, 0.6]) {
    await dragBox(page, canvas, [0.35, y], [0.65, y])
  }
  await expectShapeCount(page, initial)
})

test.describe('editing on a canvas that keeps its size', () => {
  // At the default 720 px height, selecting a shape grows the sidebar past the
  // viewport: the page, and the canvas with it, grow and the image is refitted
  // under the pointer, and "Save draft" scrolls into view. Room for the whole
  // sidebar keeps the canvas, and the image on it, where the first drag found
  // them.
  test.use({ viewport: { width: 1280, height: 1400 } })

  test('the select tool moves a box and resizes it from a corner handle', async ({
    page,
    demo,
  }) => {
    const canvas = await openItem(page, demo.id, demo.firstItemId)
    const initial = await shapeCount(page)
    await settledCanvas(page, canvas)
    const frame = (await canvas.boundingBox())!

    await page.keyboard.press('b')
    await dragBox(page, canvas, [0.3, 0.3], [0.5, 0.5])
    await expectShapeCount(page, initial + 1)

    // The new box stays selected: drag its body, then its south-east corner,
    // which is only where the press lands if the move happened.
    await page.keyboard.press('v')
    await dragBox(page, canvas, [0.4, 0.4], [0.45, 0.45])
    await dragBox(page, canvas, [0.55, 0.55], [0.65, 0.6])
    await expectShapeCount(page, initial + 1)

    const saved = page.waitForResponse(
      (response) =>
        response.url().includes(`/items/${demo.firstItemId}/annotations`) &&
        response.request().method() === 'POST',
    )
    await page.getByRole('button', { name: 'Save draft' }).click()
    const body = (await (await saved).json()) as {
      result: { shapes: Array<{ type: string; bbox?: [number, number, number, number] }> }
    }
    const boxes = body.result.shapes.filter((shape) => shape.type === 'bbox')
    expect(boxes).toHaveLength(1)
    const [x0, y0, x1, y1] = boxes[0].bbox!
    // Drawn 0.2 × 0.2 of the canvas, now 0.3 × 0.25; the image scale is the
    // same on both axes, so the aspect ratio tells the resize apart.
    const expected = (0.3 * frame.width) / (0.25 * frame.height)
    expect((x1 - x0) / (y1 - y0)).toBeCloseTo(expected, 1)
  })

  test('the superpixel tool fills a clicked superpixel into a mask; Alt+click removes it', async ({
    page,
    api,
    demo,
  }) => {
    await allowMasks(api, demo.id)
    const canvas = await openItem(page, demo.id, demo.firstItemId)
    const initial = await shapeCount(page)

    await page.keyboard.press('x')
    await expect(page.getByRole('button', { name: /^Superpixels/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    // SLIC runs in the page once the tool is picked; the storage must allow
    // the browser to read the pixels (CORS), or it ends in `error`.
    await expect(page.locator('[data-superpixels]')).toHaveAttribute('data-superpixels', 'ready', {
      timeout: 15_000,
    })
    await settledCanvas(page, canvas)

    await dragBox(page, canvas, [0.45, 0.45], [0.47, 0.47])
    await expectShapeCount(page, initial + 1)

    const saved = page.waitForResponse(
      (response) =>
        response.url().includes(`/items/${demo.firstItemId}/annotations`) &&
        response.request().method() === 'POST',
    )
    await page.getByRole('button', { name: 'Save draft' }).click()
    const body = (await (await saved).json()) as {
      result: { shapes: Array<{ type: string; rle?: { size: [number, number]; counts: number[] } }> }
    }
    const masks = body.result.shapes.filter((shape) => shape.type === 'mask')
    expect(masks).toHaveLength(1)
    const { size, counts } = masks[0].rle!
    expect(counts.reduce((a, b) => a + b, 0)).toBe(size[0] * size[1])
    expect(counts.length).toBeGreaterThan(2)
    await expect(page.getByRole('button', { name: 'Save draft' })).toBeEnabled()

    // The same superpixels, erased from the selected mask, leave it empty.
    // Saving refetches the item and so re-signs the image URL; the
    // superpixels are kept for the same object rather than recomputed.
    await expect(page.locator('[data-superpixels]')).toHaveAttribute('data-superpixels', 'ready')
    await page.keyboard.down('Alt')
    await dragBox(page, canvas, [0.45, 0.45], [0.47, 0.47])
    await page.keyboard.up('Alt')
    await expectShapeCount(page, initial)
  })
})

test('the keypoints tool places a skeleton point by point (Alt = occluded, N = skip)', async ({
  page,
  api,
  demo,
}) => {
  const schemas = (await (await api.get(`/api/v1/projects/${demo.id}/schemas`)).json()) as Array<{
    version: number
    definition: { classes: unknown[] }
  }>
  const latest = schemas.reduce((a, b) => (b.version > a.version ? b : a))
  const schema = await api.post(`/api/v1/projects/${demo.id}/schemas`, {
    data: {
      ...latest.definition,
      version: latest.version + 1,
      classes: [
        ...latest.definition.classes,
        {
          name: 'e2e_pose',
          display_name: 'Pose',
          color: '#0ea5e9',
          tools: ['keypoints'],
          attributes: [],
          skeleton: { points: ['head', 'hip', 'tail'], edges: [[0, 1], [1, 2]] },
        },
      ],
    },
  })
  expect(schema.status(), await schema.text()).toBe(201)

  const canvas = await openItem(page, demo.id, demo.firstItemId)
  const initial = await shapeCount(page)

  await page.keyboard.press('j')
  await expect(page.getByText(/Pose: place/)).toContainText('head (1/3)')
  await clickPoints(page, canvas, [[0.4, 0.3]])
  await expect(page.getByText(/Pose: place/)).toContainText('hip (2/3)')
  const box = await canvas.boundingBox()
  if (!box) throw new Error('canvas has no bounding box')
  await page.keyboard.down('Alt')
  await page.mouse.click(box.x + box.width * 0.45, box.y + box.height * 0.5)
  await page.keyboard.up('Alt')
  await expectShapeCount(page, initial)
  await page.keyboard.press('n')
  await expectShapeCount(page, initial + 1)
  // N skipped the keypoint; it must not also page to the next item.
  await expect(page).toHaveURL(new RegExp(`/annotate/${demo.firstItemId}$`))

  const saved = page.waitForResponse(
    (response) =>
      response.url().includes(`/items/${demo.firstItemId}/annotations`) &&
      response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: 'Save draft' }).click()
  const body = (await (await saved).json()) as {
    result: { shapes: Array<{ type: string; class: string; points?: number[][] }> }
  }
  const [pose] = body.result.shapes.filter((shape) => shape.type === 'keypoints')
  expect(pose.class).toBe('e2e_pose')
  expect(pose.points!.map((point) => point[2])).toEqual([2, 1, 0])
})

test('attributes and classification edited in the sidebar are saved with the draft', async ({
  page,
  api,
  demo,
}) => {
  const canvas = await openItem(page, demo.id, demo.firstItemId)
  await expect(page.getByText('Select a shape to edit its attributes.')).toBeVisible()

  // Item-level classification comes from the demo schema (`weather`).
  await page.getByLabel('weather').selectOption('rain')

  // A freshly drawn box is selected, so its class attributes appear (car → occluded).
  const initial = await shapeCount(page)
  await page.keyboard.press('b')
  await dragBox(page, canvas, [0.2, 0.2], [0.5, 0.5])
  await expectShapeCount(page, initial + 1)
  await expect(page.getByRole('heading', { name: 'Selected shape' })).toBeVisible()
  const occluded = page.getByLabel('occluded')
  await expect(occluded).not.toBeChecked()
  await occluded.check()

  const saved = page.waitForResponse(
    (response) =>
      response.url().includes(`/items/${demo.firstItemId}/annotations`) &&
      response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: 'Save draft' }).click()
  expect((await saved).ok()).toBeTruthy()

  const after = await api.get(`/api/v1/items/${demo.firstItemId}/annotations`)
  // The list is newest version first.
  const [newest] = (await after.json()) as Array<{
    result: {
      classification: Record<string, unknown>
      shapes: Array<{ type: string; attributes: Record<string, unknown> }>
    }
  }>
  expect(newest.result.classification).toEqual({ weather: 'rain' })
  expect(newest.result.shapes.some((shape) => shape.attributes.occluded === true)).toBe(true)
})

test('image adjustments filter only the image layer (IMG-5)', async ({ page, demo }) => {
  await openItem(page, demo.id, demo.firstItemId)
  const canvases = page.locator('.konvajs-content canvas')
  await expect(canvases).toHaveCount(2)
  const filters = async (): Promise<string[]> =>
    canvases.evaluateAll((nodes) => nodes.map((node) => (node as HTMLElement).style.filter))

  expect(await filters()).toEqual(['', ''])
  const stage = page.locator('.konvajs-content')
  const before = await stage.screenshot()
  await page.getByRole('button', { name: 'Image' }).click()
  const panel = page.getByRole('group', { name: 'Image adjustments' })
  await panel.getByLabel('W', { exact: true }).fill('60')
  await panel.getByLabel('Channel').selectOption('gray')

  const [media, shapes] = await filters()
  expect(media).toMatch(/^url\("?#image-adjust-/)
  expect(shapes).toBe('')
  const filterId = /#([^")]+)/.exec(media)?.[1] ?? ''
  await expect(page.locator(`filter#${filterId} feColorMatrix`)).toHaveCount(1)
  // The browser really draws it: the stage looks different on screen.
  expect((await stage.screenshot()).equals(before)).toBe(false)

  await panel.getByRole('button', { name: 'Reset' }).click()
  expect(await filters()).toEqual(['', ''])
})

test('the ruler measures and calibrates the project scale (TOOL-8)', async ({
  page,
  demo,
  api,
}) => {
  const canvas = await openItem(page, demo.id, demo.firstItemId)
  await page.getByRole('button', { name: /^Measure/ }).click()
  await dragBox(page, canvas, [0.2, 0.5], [0.6, 0.5])

  const reading = page.getByRole('region', { name: 'Measurement' })
  await expect(reading).toContainText('px, no scale set')
  await reading.getByLabel('True length').fill('100')
  await reading.getByLabel('Unit').fill('mm')
  await reading.getByRole('button', { name: 'Set scale' }).click()
  await expect(page.getByText(/mm per pixel \(this session\)/)).toBeVisible()
  await expect(reading).toContainText('100 mm')

  await page.getByRole('button', { name: 'Save as project scale' }).click()
  await expect(page.getByText('Saved as the project scale.')).toBeVisible()
  const project = (await (await api.get(`/api/v1/projects/${demo.id}`)).json()) as {
    settings: { calibration?: { units_per_pixel: number; unit: string } }
  }
  expect(project.settings.calibration?.unit).toBe('mm')
  expect(project.settings.calibration?.units_per_pixel).toBeGreaterThan(0)
})
