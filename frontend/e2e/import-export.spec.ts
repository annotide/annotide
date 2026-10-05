/**
 * Imports and exports end to end (EXP-5, EXP-6): a COCO file picked in the
 * Imports panel is previewed in a dry run, then imported as submitted
 * annotations by the compose `worker`; an export written to the result
 * connector is downloaded on the signed URL the Download button opens, and
 * holds what was imported. A failed import is re-run from its Retry button
 * (ARC-4).
 *
 * Works on a scratch project (one sample image), hard-deleted at the end;
 * the export blob stays in Azurite.
 */
import type { APIRequestContext, Page, Response } from '@playwright/test'

import {
  createScratchProject,
  expect,
  newApiContext,
  readZipEntry,
  test as base,
  waitForJob,
} from './support'

const SAMPLE_GLOB = '*/scene-001-*.jpg'

interface ImportProject {
  id: string
  itemId: string
  itemPath: string
}

interface AnnotationRow {
  source: string
  status: string
  result: { shapes: Array<{ type: string; class: string }> }
}

const test = base.extend<Record<never, never>, { importProject: ImportProject }>({
  importProject: [
    async ({ playwright }, use, workerInfo) => {
      const api = await newApiContext(playwright, workerInfo.project.use.baseURL)
      const project = await createScratchProject(
        api,
        `E2E import ${workerInfo.workerIndex}`,
        SAMPLE_GLOB,
      )
      try {
        expect(project.itemIds, `glob ${SAMPLE_GLOB} should match exactly one sample`).toHaveLength(1)
        const [itemId] = project.itemIds
        const item = (await (await api.get(`/api/v1/items/${itemId}`)).json()) as { path: string }
        await use({ id: project.id, itemId, itemPath: item.path })
      } finally {
        await api.delete(`/api/v1/projects/${project.id}`)
        await api.dispose()
      }
    },
    { scope: 'worker' },
  ],
})

// The export test downloads what the import test wrote.
test.describe.configure({ mode: 'serial' })

/** One COCO document with a single `car` box on the scratch item. */
function cocoFile(itemPath: string): string {
  return JSON.stringify({
    images: [{ id: 1, file_name: itemPath, width: 640, height: 480 }],
    categories: [{ id: 1, name: 'car' }],
    annotations: [{ id: 1, image_id: 1, category_id: 1, bbox: [40, 60, 120, 80] }],
  })
}

function importsPanel(page: Page) {
  return page.locator('section', { has: page.getByRole('heading', { name: 'Imports' }) })
}

function jobResponse(page: Page, path: string): Promise<Response> {
  return page.waitForResponse(
    (response) => response.url().endsWith(path) && response.request().method() === 'POST',
  )
}

/** Pick `contents` as a COCO file and press the panel's submit button. */
async function submitImport(
  page: Page,
  projectId: string,
  contents: string,
  button: 'Preview' | 'Import',
): Promise<string> {
  const panel = importsPanel(page)
  await panel.getByLabel('Import format').selectOption('coco')
  await panel.locator('#import-file').setInputFiles({
    name: 'annotations.json',
    mimeType: 'application/json',
    buffer: Buffer.from(contents),
  })
  await panel.getByLabel('Dry run').setChecked(button === 'Preview')

  const queued = jobResponse(page, `/projects/${projectId}/imports/upload`)
  await panel.getByRole('button', { name: button, exact: true }).click()
  const response = await queued
  expect(response.status(), await response.text()).toBe(202)
  return ((await response.json()) as { id: string }).id
}

async function annotations(api: APIRequestContext, itemId: string): Promise<AnnotationRow[]> {
  return (await (await api.get(`/api/v1/items/${itemId}/annotations`)).json()) as AnnotationRow[]
}

test('a COCO file is previewed in a dry run, then imported as submitted annotations', async ({
  page,
  api,
  importProject,
}) => {
  await page.goto(`/projects/${importProject.id}?tab=data`)
  const panel = importsPanel(page)
  const cocoText = cocoFile(importProject.itemPath)

  const dryRun = await submitImport(page, importProject.id, cocoText, 'Preview')
  await waitForJob(api, dryRun)
  await expect(panel.getByRole('listitem').first()).toContainText('coco · succeeded · 100%')
  await expect(panel.getByRole('listitem').first()).toContainText('Dry run: ')
  await expect(panel.getByRole('listitem').first()).toContainText('1/1 matched')
  expect(await annotations(api, importProject.itemId), 'a dry run writes nothing').toHaveLength(0)

  const real = await submitImport(page, importProject.id, cocoText, 'Import')
  await waitForJob(api, real)
  const newest = panel.getByRole('listitem').first()
  await expect(newest).toContainText('coco · succeeded · 100%')
  await expect(newest).toContainText('1 imported · 1/1 matched')
  await expect(newest).not.toContainText('Dry run')

  const rows = await annotations(api, importProject.itemId)
  expect(rows).toHaveLength(1)
  expect(rows[0].status).toBe('submitted')
  expect(rows[0].result.shapes).toEqual([expect.objectContaining({ type: 'bbox', class: 'car' })])
})

test('an export is downloaded from its signed URL and holds the imported box', async ({
  page,
  api,
  playwright,
  importProject,
}) => {
  await page.goto(`/projects/${importProject.id}?tab=exports`)
  const section = page.locator('section', { has: page.getByRole('heading', { name: 'Exports' }) })
  await section.getByLabel('Format').selectOption('coco')

  const queued = jobResponse(page, `/projects/${importProject.id}/exports`)
  await section.getByRole('button', { name: 'Export', exact: true }).click()
  const response = await queued
  expect(response.status(), await response.text()).toBe(202)
  const jobId = ((await response.json()) as { id: string }).id
  await waitForJob(api, jobId)

  const newest = section.getByRole('listitem').first()
  await expect(newest).toContainText('coco · succeeded · 100%')

  // Download asks the API for a signed URL and opens it in a new tab.
  const minted = page.waitForResponse((r) => r.url().endsWith(`/jobs/${jobId}/download`))
  const popup = page.waitForEvent('popup')
  await newest.getByRole('button', { name: 'Download' }).click()
  const { url } = (await (await minted).json()) as { url: string }
  await (await popup).close()

  // The signed URL alone is the credential: fetch it without the bearer token.
  const anonymous = await playwright.request.newContext()
  try {
    const blob = await anonymous.get(url)
    expect(blob.ok(), `signed export URL answered ${blob.status()}`).toBeTruthy()
    const coco = JSON.parse(readZipEntry(await blob.body(), 'coco.json').toString('utf8')) as {
      images: Array<{ id: number; file_name: string }>
      annotations: Array<{ image_id: number; category_id: number; bbox: number[] }>
      categories: Array<{ id: number; name: string }>
    }
    const image = coco.images.find((candidate) => candidate.file_name === importProject.itemPath)
    expect(image, `export lists ${importProject.itemPath}`).toBeDefined()
    const car = coco.categories.find((category) => category.name === 'car')
    const boxes = coco.annotations.filter((annotation) => annotation.image_id === image?.id)
    expect(boxes).toEqual([expect.objectContaining({ category_id: car?.id })])
  } finally {
    await anonymous.dispose()
  }
})

test('a failed import shows its error and is re-run from the Retry button', async ({
  page,
  api,
  importProject,
}) => {
  await page.goto(`/projects/${importProject.id}?tab=data`)
  const panel = importsPanel(page)

  const jobId = await submitImport(page, importProject.id, '{"images": []}', 'Import')
  await expect
    .poll(async () => ((await (await api.get(`/api/v1/jobs/${jobId}`)).json()) as { status: string }).status)
    .toBe('failed')
  const newest = panel.getByRole('listitem').first()
  await expect(newest).toContainText('coco · failed')
  await expect(newest).toContainText('no COCO-format JSON file found')

  const retried = jobResponse(page, `/jobs/${jobId}/retry`)
  await newest.getByRole('button', { name: 'Retry' }).click()
  const response = await retried
  expect(response.ok(), await response.text()).toBeTruthy()
  expect(((await response.json()) as { status: string }).status).toBe('queued')

  // Same payload, same file: it fails the same way, and can be retried again.
  await expect
    .poll(async () => ((await (await api.get(`/api/v1/jobs/${jobId}`)).json()) as { status: string }).status)
    .toBe('failed')
  await expect(newest).toContainText('coco · failed')
  await expect(newest.getByRole('button', { name: 'Retry' })).toBeEnabled()
})
