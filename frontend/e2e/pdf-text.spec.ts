/**
 * PDF text mode (CONTRACTS "PDF text mode"): in a project with
 * `settings.pdf_mode: text` a scanned PDF becomes a text item, the worker
 * extracts its text once onto the result connector, and the ordinary text
 * annotator labels it with code-point offsets.
 */
import type { APIRequestContext } from '@playwright/test'

import { createScratchProject, expect, newApiContext, test as base, twoPagePdf, waitForJob } from './support'

const SCHEMA = {
  version: 1,
  classes: [{ name: 'PER', display_name: 'Person', color: '#f59e0b', tools: ['span'], attributes: [] }],
  classification: [],
}

/** What PDFium reads from `twoPagePdf()`: one line per page, pages joined by a blank line. */
const TEXT = 'Cover page\n\nInvoice total 42,00'

interface ItemRow {
  id: string
  media_type: string
  media_url: string | null
  meta: { pdf_text?: { status: string; pages?: number[][] }; views?: Array<{ path: string; label?: string }> }
}

interface AnnotationRow {
  result: { media_type: string; shapes: Array<{ type: string; class: string; start?: number; end?: number }> }
}

async function uploadAndScan(api: APIRequestContext, projectId: string): Promise<void> {
  const minted = await api.post(`/api/v1/projects/${projectId}/uploads`, {
    data: { files: [{ path: 'invoice.pdf', content_type: 'application/pdf' }] },
  })
  expect(minted.status(), await minted.text()).toBe(200)
  const { uploads } = (await minted.json()) as {
    uploads: Array<{ url: string; method: string; headers: Record<string, string> }>
  }
  const put = await api.fetch(uploads[0].url, {
    method: uploads[0].method,
    headers: uploads[0].headers,
    data: twoPagePdf(),
  })
  expect(put.ok(), `upload answered ${put.status()}`).toBeTruthy()
  const scan = await api.post(`/api/v1/projects/${projectId}/scan`, { data: {} })
  expect(scan.status(), await scan.text()).toBe(202)
  await waitForJob(api, ((await scan.json()) as { id: string }).id)
}

interface TextModeProject {
  id: string
  itemId: string
}

const test = base.extend<Record<never, never>, { textModeProject: TextModeProject }>({
  textModeProject: [
    async ({ playwright }, use, workerInfo) => {
      const api = await newApiContext(playwright, workerInfo.project.use.baseURL)
      const prefix = `e2e-pdf-text/${workerInfo.workerIndex}-${Date.now()}/`
      const project = await createScratchProject(api, 'E2E pdf text', '*.pdf', prefix)
      try {
        const patched = await api.patch(`/api/v1/projects/${project.id}`, {
          data: { settings: { pdf_mode: 'text' } },
        })
        expect(patched.status(), await patched.text()).toBe(200)
        const schema = await api.post(`/api/v1/projects/${project.id}/schemas`, { data: SCHEMA })
        expect(schema.status(), await schema.text()).toBe(201)
        await uploadAndScan(api, project.id)

        // The scan queues `extract_text`; the item is ready once its text is.
        let item: ItemRow | undefined
        await expect
          .poll(
            async () => {
              const listed = (await (await api.get(`/api/v1/projects/${project.id}/items`)).json()) as {
                items: ItemRow[]
              }
              item = listed.items[0]
              return item?.meta.pdf_text?.status
            },
            { timeout: 30_000 },
          )
          .toBe('ready')
        expect(item?.media_type).toBe('text')
        await use({ id: project.id, itemId: (item as ItemRow).id })
      } finally {
        await api.delete(`/api/v1/projects/${project.id}`)
        await api.dispose()
      }
    },
    { scope: 'worker' },
  ],
})

test('extracts the text once and serves it instead of the PDF', async ({ api, textModeProject }) => {
  const item = (await (await api.get(`/api/v1/items/${textModeProject.itemId}`)).json()) as ItemRow
  expect(item.meta.pdf_text?.pages).toEqual([
    [0, 10],
    [12, 31],
  ])
  expect(item.meta.views?.map((view) => view.path.split('/').pop())).toEqual(['invoice.pdf'])
  expect(item.media_url).toContain(`text/${textModeProject.itemId}.txt`)
  const text = await (await api.fetch(item.media_url as string)).text()
  expect(text).toBe(TEXT)
})

test('labels the extracted text with code-point offsets', async ({ page, api, textModeProject }) => {
  await page.goto(`/projects/${textModeProject.id}/annotate/${textModeProject.itemId}`)
  const body = page.getByTestId('text-body')
  await expect(body).toContainText('Invoice total 42,00')
  // Each page of the extracted text is marked, and the original PDF is
  // rendered page by page beside it (pdf.js, as in layout mode).
  await expect(body.getByTestId('page-marker')).toHaveCount(2)
  await expect(page.getByTestId('pdf-view-page')).toHaveText('Page 1 of 2')
  await page.getByRole('button', { name: 'Next page' }).click()
  await expect(page.getByTestId('pdf-view-page')).toHaveText('Page 2 of 2')
  // The PDF follows the text: a page marker turns it back.
  await body.getByRole('button', { name: 'Show page 1 of the PDF' }).click()
  await expect(page.getByTestId('pdf-view-page')).toHaveText('Page 1 of 2')

  await page.getByRole('button', { name: /Person/ }).click()
  await body.getByText(/Cover page/).dblclick({ position: { x: 8, y: 8 } })
  await expect(body.getByText('Cover', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: /Save draft/ }).click()
  await expect
    .poll(async () => {
      const response = await api.get(`/api/v1/items/${textModeProject.itemId}/annotations`)
      return ((await response.json()) as AnnotationRow[]).length
    })
    .toBeGreaterThan(0)
  const [saved] = (await (
    await api.get(`/api/v1/items/${textModeProject.itemId}/annotations`)
  ).json()) as AnnotationRow[]
  expect(saved.result.media_type).toBe('text')
  const spans = saved.result.shapes.filter((shape) => shape.type === 'span')
  expect(spans.map(({ class: cls, start, end }) => [cls, start, end])).toEqual([['PER', 0, 5]])
})

test('an owner can queue the extraction again from the project settings', async ({ page, textModeProject }) => {
  await page.goto(`/projects/${textModeProject.id}/settings`)
  await page.getByRole('button', { name: 'Extract PDF text' }).click()
  await expect(page.getByText('Extraction queued.')).toBeVisible()
})
