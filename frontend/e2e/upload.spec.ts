/**
 * Browser upload end to end (§12 upload path): files and a whole folder
 * picked in the Upload panel go straight from the browser to Azurite on
 * write-scoped signed URLs, the panel queues a scan, and the compose
 * `worker` registers them as items. The API never sees the bytes.
 *
 * Works on a scratch project with a source prefix of its own, so the
 * uploads stay out of the demo's `samples/`; the project is hard-deleted at
 * the end, the uploaded blobs stay in Azurite under `e2e-upload/`.
 */
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import type { APIRequestContext, Page } from '@playwright/test'

import {
  createScratchProject,
  expect,
  newApiContext,
  test as base,
  waitForJob,
} from './support'

/** A valid 1×1 PNG: enough for the scan to register it as an image item. */
const PNG = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  'base64',
)

interface UploadProject {
  id: string
  prefix: string
}

const test = base.extend<Record<never, never>, { uploadProject: UploadProject }>({
  uploadProject: [
    async ({ playwright }, use, workerInfo) => {
      const api = await newApiContext(playwright, workerInfo.project.use.baseURL)
      const prefix = `e2e-upload/${workerInfo.workerIndex}-${Date.now()}/`
      const project = await createScratchProject(api, 'E2E upload', '*.png', prefix)
      try {
        expect(project.itemIds, 'a fresh prefix starts empty').toHaveLength(0)
        await use({ id: project.id, prefix })
      } finally {
        await api.delete(`/api/v1/projects/${project.id}`)
        await api.dispose()
      }
    },
    { scope: 'worker' },
  ],
})

async function itemPaths(api: APIRequestContext, projectId: string): Promise<string[]> {
  const page = (await (
    await api.get(`/api/v1/projects/${projectId}/items`, { params: { limit: 100 } })
  ).json()) as { items: Array<{ path: string }> }
  return page.items.map((item) => item.path)
}

/** Click Upload; resolve once the scan the panel queued afterwards has finished. */
async function uploadAndScan(page: Page, api: APIRequestContext, projectId: string, count: number) {
  const panel = page.locator('section', { has: page.getByRole('heading', { name: 'Upload' }) })
  const scanQueued = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/projects/${projectId}/scan`) &&
      response.request().method() === 'POST',
  )
  await panel.getByRole('button', { name: `Upload ${count}` }).click()

  const scan = await scanQueued
  expect(scan.status(), await scan.text()).toBe(202)
  await waitForJob(api, ((await scan.json()) as { id: string }).id)
  // The panel follows the scan it queued and says when it is done.
  await expect(panel.getByRole('status')).toHaveText(`${count} uploaded · scan finished`)
}

test('files picked in the Upload panel become items of the project', async ({
  page,
  api,
  uploadProject,
}) => {
  await page.goto(`/projects/${uploadProject.id}?tab=data`)
  const panel = page.locator('section', { has: page.getByRole('heading', { name: 'Upload' }) })

  await panel.getByLabel('Choose files').setInputFiles([
    { name: 'left.png', mimeType: 'image/png', buffer: PNG },
    { name: 'right.png', mimeType: 'image/png', buffer: PNG },
  ])
  await expect(panel.getByTestId('upload-selection')).toContainText('2 files')
  await expect(panel.getByTestId('upload-selection')).toContainText('left.png, right.png')

  await uploadAndScan(page, api, uploadProject.id, 2)

  expect(await itemPaths(api, uploadProject.id)).toEqual(
    expect.arrayContaining([`${uploadProject.prefix}left.png`, `${uploadProject.prefix}right.png`]),
  )
  // The finished scan refreshed the item grid: no reload needed.
  await page.getByRole('tab', { name: 'Items' }).click()
  await expect(page.getByText('left.png', { exact: true })).toBeVisible()
})

test('a picked folder keeps its relative paths in the store', async ({
  page,
  api,
  uploadProject,
}) => {
  const root = mkdtempSync(join(tmpdir(), 'e2e-upload-'))
  const folder = join(root, 'batch')
  mkdirSync(join(folder, 'road'), { recursive: true })
  writeFileSync(join(folder, 'top.png'), PNG)
  writeFileSync(join(folder, 'road', 'deep.png'), PNG)

  try {
    await page.goto(`/projects/${uploadProject.id}?tab=data`)
    const panel = page.locator('section', { has: page.getByRole('heading', { name: 'Upload' }) })

    // `webkitdirectory` input: Playwright hands Chromium the whole tree.
    await panel.getByLabel('Choose a folder').setInputFiles(folder)
    await expect(panel.getByTestId('upload-selection')).toContainText('2 files')
    await expect(panel.getByTestId('upload-selection')).toContainText('batch/road/deep.png')

    await uploadAndScan(page, api, uploadProject.id, 2)

    expect(await itemPaths(api, uploadProject.id)).toEqual(
      expect.arrayContaining([
        `${uploadProject.prefix}batch/top.png`,
        `${uploadProject.prefix}batch/road/deep.png`,
      ]),
    )
  } finally {
    rmSync(root, { recursive: true, force: true })
  }
})
