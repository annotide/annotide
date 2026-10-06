/**
 * Quality control and region split end to end (QA-1, QA-2, IMG-6): set
 * consensus in the project settings, see the Quality panel, split an image
 * into region tasks from the item grid, and open a region task in the
 * annotator.
 *
 * Works on a scratch project with one sample image, like `workflow.spec.ts`,
 * so the demo project's queue and settings are left alone.
 */
import { annotatorCanvas, createScratchProject, expect, newApiContext, test as base } from './support'

interface ScratchImage {
  id: string
  itemId: string
}

interface TaskRow {
  id: string
  item_id: string
  type: 'annotate' | 'review'
  status: 'open' | 'in_progress' | 'done' | 'cancelled'
  region: [number, number, number, number] | null
}

const SAMPLE_GLOB = '*/scene-002-*.jpg'

const test = base.extend<Record<never, never>, { scratch: ScratchImage }>({
  scratch: [
    async ({ playwright }, use, workerInfo) => {
      const api = await newApiContext(playwright, workerInfo.project.use.baseURL)
      const project = await createScratchProject(
        api,
        `E2E quality ${workerInfo.workerIndex}`,
        SAMPLE_GLOB,
      )
      try {
        expect(project.itemIds, `glob ${SAMPLE_GLOB} should match exactly one sample`).toHaveLength(1)
        await use({ id: project.id, itemId: project.itemIds[0] })
      } finally {
        await api.delete(`/api/v1/projects/${project.id}`)
        await api.dispose()
      }
    },
    { scope: 'worker' },
  ],
})

test.describe.configure({ mode: 'serial' })

test('consensus setting saves and the Quality panel explains the empty state', async ({
  page,
  scratch,
}) => {
  await page.goto(`/projects/${scratch.id}/settings`)
  await page.getByLabel('Annotators per item (consensus)').selectOption('2')
  await page.getByRole('button', { name: 'Save workflow' }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Saved.' })).toBeVisible()

  await page.goto(`/projects/${scratch.id}?tab=tasks`)
  await expect(page.getByRole('heading', { name: 'Quality' })).toBeVisible()
  await expect(page.getByText(/No item has two or more consensus versions/)).toBeVisible()

  // Split refuses consensus projects; put the setting back for the next test.
  await page.goto(`/projects/${scratch.id}/settings`)
  await page.getByLabel('Annotators per item (consensus)').selectOption('1')
  await page.getByRole('button', { name: 'Save workflow' }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Saved.' })).toBeVisible()
})

test('splits an image into region tasks and opens one in the annotator', async ({
  page,
  api,
  scratch,
}) => {
  await page.goto(`/projects/${scratch.id}`)
  await page.getByRole('checkbox', { name: 'Select all loaded items' }).check()
  await page.getByLabel('rows').selectOption('1')
  await page.getByLabel('columns').selectOption('2')
  await page.getByRole('button', { name: 'Split into region tasks' }).click()
  await expect(page.getByRole('status').filter({ hasText: 'Split 1 item.' })).toBeVisible()

  const response = await api.get(`/api/v1/projects/${scratch.id}/tasks`, { params: { limit: 100 } })
  const tasks = ((await response.json()) as { items: TaskRow[] }).items
  const regions = tasks.filter((task) => task.status === 'open' && task.region !== null)
  expect(regions).toHaveLength(2)

  await page.goto(`/projects/${scratch.id}/annotate`)
  await expect(page).toHaveURL(new RegExp(`/projects/${scratch.id}/annotate/${scratch.itemId}$`))
  await annotatorCanvas(page)
  await expect(page.getByTestId('work-note')).toContainText('Region task')

  // Hand the task back so the scratch project is deleted without a live lock.
  await page.getByRole('button', { name: 'Skip' }).click()
})
