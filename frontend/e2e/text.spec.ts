/**
 * Text annotation end to end (TOOL): upload a text file, give the project a
 * schema with span and relation classes, mark two entities and link them in
 * the annotator, save, and check the stored spans are code-point offsets.
 *
 * The file goes to the demo connector under a prefix of its own, so the demo
 * project (which scans all of `samples/`) never sees it.
 */
import type { APIRequestContext } from '@playwright/test'

import { createScratchProject, expect, newApiContext, test as base, waitForJob } from './support'

// "😀" is two UTF-16 units but one code point: "Contoso" starts at code
// point 17, UTF-16 offset 18.
const TEXT = 'Alice 😀 works at Contoso.'

const SCHEMA = {
  version: 1,
  classes: [
    {
      name: 'PER',
      display_name: 'Person',
      color: '#e11d48',
      hotkey: '1',
      tools: ['span'],
      attributes: [],
    },
    {
      name: 'ORG',
      display_name: 'Organisation',
      color: '#2563eb',
      hotkey: '2',
      tools: ['span'],
      attributes: [],
    },
    {
      name: 'works_for',
      display_name: 'works for',
      color: '#16a34a',
      tools: ['relation'],
      attributes: [],
    },
  ],
  classification: [],
}

interface TextProject {
  id: string
  itemId: string
}

interface AnnotationRow {
  kind: string
  result: {
    media_type: string
    shapes: Array<{
      id: string
      type: string
      class: string
      start?: number
      end?: number
      from?: string
      to?: string
    }>
  }
}

async function uploadText(api: APIRequestContext, projectId: string): Promise<void> {
  const minted = await api.post(`/api/v1/projects/${projectId}/uploads`, {
    data: { files: [{ path: 'note.txt', content_type: 'text/plain; charset=utf-8' }] },
  })
  expect(minted.status(), await minted.text()).toBe(200)
  const { uploads } = (await minted.json()) as {
    uploads: Array<{ url: string; method: string; headers: Record<string, string> }>
  }
  const target = uploads[0]
  const put = await api.fetch(target.url, {
    method: target.method,
    headers: target.headers,
    data: Buffer.from(TEXT, 'utf8'),
  })
  expect(put.ok(), `upload answered ${put.status()}`).toBeTruthy()
  const scan = await api.post(`/api/v1/projects/${projectId}/scan`, { data: {} })
  expect(scan.status(), await scan.text()).toBe(202)
  await waitForJob(api, ((await scan.json()) as { id: string }).id)
}

const test = base.extend<Record<never, never>, { textProject: TextProject }>({
  textProject: [
    async ({ playwright }, use, workerInfo) => {
      const api = await newApiContext(playwright, workerInfo.project.use.baseURL)
      const prefix = `e2e-text/${workerInfo.workerIndex}-${Date.now()}/`
      const project = await createScratchProject(api, 'E2E text', '*.txt', prefix)
      try {
        const schema = await api.post(`/api/v1/projects/${project.id}/schemas`, { data: SCHEMA })
        expect(schema.status(), await schema.text()).toBe(201)
        await uploadText(api, project.id)
        const items = (await (await api.get(`/api/v1/projects/${project.id}/items`)).json()) as {
          items: Array<{ id: string; media_type: string }>
        }
        expect(items.items.map((item) => item.media_type)).toEqual(['text'])
        await use({ id: project.id, itemId: items.items[0].id })
      } finally {
        await api.delete(`/api/v1/projects/${project.id}`)
        await api.dispose()
      }
    },
    { scope: 'worker' },
  ],
})

test('marks spans, links them and saves code-point offsets', async ({ page, api, textProject }) => {
  await page.goto(`/projects/${textProject.id}/annotate/${textProject.itemId}`)
  const body = page.getByTestId('text-body')
  await expect(body).toHaveText(TEXT)

  // A double-click selects one word; releasing the mouse makes it a span.
  await page.getByRole('button', { name: /Person/ }).click()
  await body.getByText(TEXT).dblclick({ position: { x: 8, y: 8 } })
  await expect(body.getByText('Alice', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: /Organisation/ }).click()
  // "Contoso" sits near the end of the line: select it by double-click there.
  const contoso = await page.evaluate(() => {
    const el = document.querySelector('[data-testid="text-body"]')
    const node = el?.lastChild?.firstChild ?? el?.lastChild
    if (!node || !node.textContent) return null
    const at = node.textContent.indexOf('Contoso')
    const range = document.createRange()
    range.setStart(node, at + 2)
    range.setEnd(node, at + 3)
    const rect = range.getBoundingClientRect()
    return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 }
  })
  if (!contoso) throw new Error('Contoso not found')
  await page.mouse.dblclick(contoso.x, contoso.y)
  await expect(body.getByText('Contoso', { exact: true })).toBeVisible()

  await body.getByText('Alice', { exact: true }).click()
  await page.getByRole('button', { name: /Link/ }).click()
  await body.getByText('Contoso', { exact: true }).click()
  await expect(page.getByRole('region', { name: 'Relations' })).toContainText(
    'Person “Alice” —works for→ Organisation “Contoso”',
  )

  await page.getByRole('button', { name: /Save draft/ }).click()
  await expect
    .poll(async () => {
      const response = await api.get(`/api/v1/items/${textProject.itemId}/annotations`)
      return ((await response.json()) as AnnotationRow[]).length
    })
    .toBeGreaterThan(0)

  const [saved] = (await (
    await api.get(`/api/v1/items/${textProject.itemId}/annotations`)
  ).json()) as AnnotationRow[]
  expect(saved.kind).toBe('primary')
  expect(saved.result.media_type).toBe('text')
  const spans = saved.result.shapes.filter((shape) => shape.type === 'span')
  expect(spans.map(({ class: cls, start, end }) => [cls, start, end]).sort()).toEqual([
    ['ORG', 17, 24],
    ['PER', 0, 5],
  ])
  const relation = saved.result.shapes.find((shape) => shape.type === 'relation')
  const per = spans.find((shape) => shape.class === 'PER')
  const org = spans.find((shape) => shape.class === 'ORG')
  expect(relation).toMatchObject({ class: 'works_for', from: per?.id, to: org?.id })
})
