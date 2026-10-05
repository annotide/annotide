/**
 * PDF items (TOOL): pdf.js renders the document from its signed URL in the
 * browser, a box drawn on page 2 is stored in that page's points with the
 * words under it.
 */
import type { APIRequestContext } from '@playwright/test'

import {
  createScratchProject,
  DEMO_MODEL_NAME,
  expect,
  newApiContext,
  test as base,
  twoPagePdf,
  waitForJob,
} from './support'

const SCHEMA = {
  version: 1,
  classes: [
    { name: 'total', display_name: 'Total', color: '#e11d48', tools: ['bbox'], attributes: [] },
    // Predicted by the reference model's PDF field rules (model-service doc_fields).
    { name: 'amount', display_name: 'Amount', color: '#2563eb', tools: ['bbox'], attributes: [] },
    // NER over the text layer: a span stores one box per line and the words' text.
    { name: 'PER', display_name: 'Person', color: '#f59e0b', tools: ['span'], attributes: [] },
    { name: 'about', display_name: 'About', color: '#0ea5e9', tools: ['relation'], attributes: [] },
  ],
  classification: [],
}

async function uploadPdf(api: APIRequestContext, projectId: string): Promise<void> {
  const minted = await api.post(`/api/v1/projects/${projectId}/uploads`, {
    data: { files: [{ path: 'invoice.pdf', content_type: 'application/pdf' }] },
  })
  expect(minted.status(), await minted.text()).toBe(200)
  const { uploads } = (await minted.json()) as {
    uploads: Array<{ url: string; method: string; headers: Record<string, string> }>
  }
  const target = uploads[0]
  const put = await api.fetch(target.url, {
    method: target.method,
    headers: target.headers,
    data: twoPagePdf(),
  })
  expect(put.ok(), `upload answered ${put.status()}`).toBeTruthy()
  const scan = await api.post(`/api/v1/projects/${projectId}/scan`, { data: {} })
  expect(scan.status(), await scan.text()).toBe(202)
  await waitForJob(api, ((await scan.json()) as { id: string }).id)
}

interface PdfProject {
  id: string
  itemId: string
}

const test = base.extend<Record<never, never>, { pdfProject: PdfProject }>({
  pdfProject: [
    async ({ playwright }, use, workerInfo) => {
      const api = await newApiContext(playwright, workerInfo.project.use.baseURL)
      const prefix = `e2e-pdf/${workerInfo.workerIndex}-${Date.now()}/`
      const project = await createScratchProject(api, 'E2E pdf', '*.pdf', prefix)
      try {
        const schema = await api.post(`/api/v1/projects/${project.id}/schemas`, { data: SCHEMA })
        expect(schema.status(), await schema.text()).toBe(201)
        await uploadPdf(api, project.id)
        const items = (await (await api.get(`/api/v1/projects/${project.id}/items`)).json()) as {
          items: Array<{ id: string; media_type: string }>
        }
        expect(items.items.map((item) => item.media_type)).toEqual(['pdf'])
        await use({ id: project.id, itemId: items.items[0].id })
      } finally {
        await api.delete(`/api/v1/projects/${project.id}`)
        await api.dispose()
      }
    },
    { scope: 'worker' },
  ],
})

// Pre-labelling only targets untouched items, so it runs before the drawing test.
test.describe.configure({ mode: 'serial' })

test('measures the document and pre-labels its amounts on the right page', async ({
  page,
  api,
  pdfProject,
}) => {
  // The thumbnail job after the scan rendered page 1 and measured the file.
  await expect
    .poll(async () => {
      const item = (await (await api.get(`/api/v1/items/${pdfProject.itemId}`)).json()) as {
        meta: Record<string, unknown>
      }
      return item.meta.page_count
    })
    .toBe(2)
  const item = (await (await api.get(`/api/v1/items/${pdfProject.itemId}`)).json()) as {
    meta: { page_sizes: number[][]; text_layer: boolean }
    thumbnail_url: string | null
  }
  expect(item.meta.page_sizes).toEqual([
    [600, 800],
    [600, 800],
  ])
  expect(item.meta.text_layer).toBe(true)
  expect(item.thumbnail_url).toBeTruthy()

  // A model of its own on the compose model service, with no class mapping:
  // the project schema is sent as is, so the model may emit `amount`. (The
  // demo version maps only `object` → `car`.)
  const models = (await (await api.get('/api/v1/models', { params: { limit: 100 } })).json()) as {
    items: Array<{ id: string; name: string; endpoint_url: string }>
  }
  const demo = models.items.find((candidate) => candidate.name === DEMO_MODEL_NAME)
  if (!demo) throw new Error(`"${DEMO_MODEL_NAME}" not registered — run \`make seed\`.`)
  const created = await api.post('/api/v1/models', {
    data: { name: `E2E pdf fields ${Date.now()}`, task: 'detect', endpoint_url: demo.endpoint_url },
  })
  expect(created.status(), await created.text()).toBe(201)
  const modelId = ((await created.json()) as { id: string }).id
  try {
    const version = await api.post(`/api/v1/models/${modelId}/versions`, {
      data: { class_mapping: {} },
    })
    expect(version.status(), await version.text()).toBe(201)
    const queued = await api.post(`/api/v1/projects/${pdfProject.id}/prelabel`, {
      data: { model_version_id: ((await version.json()) as { id: string }).id },
    })
    expect(queued.status(), await queued.text()).toBe(202)
    await waitForJob(api, ((await queued.json()) as { id: string }).id)
  } finally {
    // A model that wrote pre-labels is soft-deleted; this used to be a 500.
    const deleted = await api.delete(`/api/v1/models/${modelId}`)
    expect(deleted.status(), await deleted.text()).toBe(204)
  }

  const annotations = (await (
    await api.get(`/api/v1/items/${pdfProject.itemId}/annotations`)
  ).json()) as Array<{
    source: string
    result: { shapes: Array<{ class: string; page: number; bbox: number[]; text?: string }> }
  }>
  const predicted = annotations.find((row) => row.source === 'model')
  expect(predicted, 'no model version').toBeTruthy()
  const amount = predicted!.result.shapes.find((shape) => shape.class === 'amount')
  expect(amount).toMatchObject({ page: 2, text: '42,00' })
  // PDFium (model) and pdf.js (browser) agree on where the line is: the
  // first test's box around it spanned y 80–110 and found its words.
  const [, top, , bottom] = amount!.bbox
  expect(top).toBeGreaterThan(80)
  expect(bottom).toBeLessThan(110)

  // The annotator opens on the model's draft: its box is on page 2, with its text.
  const amountId = (amount as unknown as { id: string }).id
  await page.goto(`/projects/${pdfProject.id}/annotate/${pdfProject.itemId}`)
  await expect(page.getByTestId(`pdf-box-${amountId}`)).toHaveCount(0)
  await page.getByRole('button', { name: 'Next page' }).click()
  await page.getByTestId(`pdf-box-${amountId}`).dispatchEvent('mousedown')
  await expect(page.getByTestId('pdf-selection-text')).toHaveText('42,00')
})

test('draws a box on page 2 and saves it with the words under it', async ({
  page,
  api,
  pdfProject,
}) => {
  await page.goto(`/projects/${pdfProject.id}/annotate/${pdfProject.itemId}`)
  await expect(page.getByTestId('page-counter')).toHaveText('Page 1 / 2')
  await page.getByRole('button', { name: 'Next page' }).click()
  await expect(page.getByTestId('page-counter')).toHaveText('Page 2 / 2')

  // pdf.js drew something: the canvas is not blank.
  const canvas = page.getByTestId('pdf-canvas')
  await expect
    .poll(() =>
      canvas.evaluate((element: HTMLCanvasElement) => {
        const { data } = element.getContext('2d')!.getImageData(0, 0, element.width, element.height)
        return data.some((value, index) => index % 4 !== 3 && value < 200)
      }),
    )
    .toBe(true)

  // Drag around the text line, in page points scaled to the overlay's size.
  const overlay = page.getByTestId('pdf-overlay')
  const box = (await overlay.boundingBox())!
  const toScreen = (x: number, y: number) => ({
    x: box.x + (x / 600) * box.width,
    y: box.y + (y / 800) * box.height,
  })
  const from = toScreen(90, 80)
  const to = toScreen(260, 110)
  await page.mouse.move(from.x, from.y)
  await page.mouse.down()
  await page.mouse.move(to.x, to.y, { steps: 5 })
  await page.mouse.up()
  await expect(page.getByTestId('pdf-selection-text')).toHaveText('Invoice total 42,00')

  // Moving the box off the line drops its text; moving it back reads it again.
  // Sideways, along the line's height: lower down the page is scrolled out of
  // view at the default viewport, where the pointer no longer hits the box.
  const moveBox = async (fromX: number, toX: number) => {
    const start = toScreen(fromX, 95)
    const end = toScreen(toX, 95)
    await page.mouse.move(start.x, start.y)
    await page.mouse.down()
    await page.mouse.move(end.x, end.y, { steps: 5 })
    await page.mouse.up()
  }
  await moveBox(175, 475)
  await expect(page.getByTestId('pdf-selection-text')).toHaveCount(0)
  await moveBox(475, 175)
  await expect(page.getByTestId('pdf-selection-text')).toHaveText('Invoice total 42,00')

  await page.getByRole('button', { name: /Save draft/ }).click()
  // The model's draft is already there (previous test): wait for ours.
  await expect
    .poll(async () => {
      const response = await api.get(`/api/v1/items/${pdfProject.itemId}/annotations`)
      return ((await response.json()) as Array<{ source: string }>).some(
        (row) => row.source === 'human',
      )
    })
    .toBe(true)
  const rows = (await (
    await api.get(`/api/v1/items/${pdfProject.itemId}/annotations`)
  ).json()) as Array<{
    source: string
    result: {
      media_type: string
      shapes: Array<{ class: string; page: number; bbox: number[]; text?: string }>
    }
  }>
  const saved = rows.find((row) => row.source === 'human')!
  expect(saved.result.media_type).toBe('pdf')
  // The model's `amount` box (previous test) came along from the draft it opened on.
  const shape = saved.result.shapes.find((candidate) => candidate.class === 'total')!
  expect(shape).toMatchObject({ page: 2, text: 'Invoice total 42,00' })
  // Stored in points, whatever size the page was drawn at.
  expect(shape.bbox[0]).toBeCloseTo(90, -1)
  expect(shape.bbox[3]).toBeCloseTo(110, -1)
})

test('marks a span by dragging across words, links it to a box, and keeps both after a reload', async ({
  page,
  api,
  pdfProject,
}) => {
  await page.goto(`/projects/${pdfProject.id}/annotate/${pdfProject.itemId}`)
  await page.getByRole('button', { name: 'Next page' }).click()
  await expect(page.getByTestId('page-counter')).toHaveText('Page 2 / 2')
  await page.getByRole('button', { name: 'Span' }).click()

  const overlay = page.getByTestId('pdf-overlay')
  const box = (await overlay.boundingBox())!
  const toScreen = (x: number, y: number) => ({
    x: box.x + (x / 600) * box.width,
    y: box.y + (y / 800) * box.height,
  })
  // From inside "Invoice" to inside "total" on the line at y 88-100.
  const from = toScreen(110, 94)
  const to = toScreen(160, 94)
  await page.mouse.move(from.x, from.y)
  await page.mouse.down()
  await page.mouse.move(to.x, to.y, { steps: 5 })
  await page.mouse.up()
  await expect(page.getByTestId('pdf-selection-text')).toHaveText('Invoice total')

  // Link the span to the box drawn in the previous test.
  await page.getByRole('button', { name: /Link/ }).click()
  await page.locator('[data-testid^="pdf-box-"]').first().dispatchEvent('mousedown')
  await expect(page.getByRole('region', { name: 'Relations' })).toBeVisible()

  await page.getByRole('button', { name: /Save draft/ }).click()
  let saved: { shapes: Array<Record<string, unknown>> } | undefined
  await expect
    .poll(async () => {
      const rows = (await (
        await api.get(`/api/v1/items/${pdfProject.itemId}/annotations`)
      ).json()) as Array<{ source: string; result: { shapes: Array<Record<string, unknown>> } }>
      saved = rows.find(
        (row) => row.source === 'human' && row.result.shapes.some((s) => s.type === 'span'),
      )?.result
      return saved !== undefined
    })
    .toBe(true)
  const stored = saved!.shapes.find((shape) => shape.type === 'span')!
  expect(stored).toMatchObject({ class: 'PER', page: 2, text: 'Invoice total' })
  expect(stored.boxes).toHaveLength(1)
  expect(stored.start ?? null).toBeNull()
  expect(saved!.shapes.some((shape) => shape.type === 'relation' && shape.from === stored.id)).toBe(
    true,
  )

  await page.reload()
  await page.getByRole('button', { name: 'Next page' }).click()
  await expect(page.getByTestId(`pdf-span-${String(stored.id)}`)).toBeVisible()
  await expect(page.getByRole('region', { name: 'Relations' })).toBeVisible()
})
