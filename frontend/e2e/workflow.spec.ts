/**
 * The task queue and review flow end to end (WF-1…WF-4): claim from the
 * annotate queue, submit, claim from the review queue, reject back to the
 * annotator, re-submit, approve.
 *
 * The demo project's queue is shared with everything else, so this file works
 * on a project of its own: the same connector and label schema as the demo,
 * but a `source_glob` that admits exactly one sample image. The project is
 * created and scanned through the API once per worker and deleted afterwards
 * (hard delete; items, tasks and annotations cascade).
 */
import type { APIRequestContext, Locator, Page } from '@playwright/test'

import {
  annotatorCanvas,
  createScratchProject,
  dragBox,
  expect,
  newApiContext,
  test as base,
} from './support'

interface WorkflowProject {
  id: string
  itemId: string
}

interface TaskRow {
  id: string
  item_id: string
  type: 'annotate' | 'review'
  status: 'open' | 'in_progress' | 'done' | 'cancelled'
  assignee_id: string | null
  locked_by_id: string | null
}

interface AnnotationRow {
  id: string
  version: number
  status: 'draft' | 'submitted' | 'approved' | 'rejected'
  source: 'human' | 'model'
  result: { shapes: Array<{ type: string }> }
}

const SAMPLE_GLOB = '*/scene-001-*.jpg'

const test = base.extend<Record<never, never>, { workflow: WorkflowProject }>({
  workflow: [
    async ({ playwright }, use, workerInfo) => {
      const api = await newApiContext(playwright, workerInfo.project.use.baseURL)
      const project = await createScratchProject(
        api,
        `E2E workflow ${workerInfo.workerIndex}`,
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

// One item walks through the whole lifecycle; every test depends on the last.
test.describe.configure({ mode: 'serial' })

async function tasks(api: APIRequestContext, projectId: string): Promise<TaskRow[]> {
  const response = await api.get(`/api/v1/projects/${projectId}/tasks`, { params: { limit: 100 } })
  expect(response.ok()).toBeTruthy()
  return ((await response.json()) as { items: TaskRow[] }).items
}

async function versions(api: APIRequestContext, itemId: string): Promise<AnnotationRow[]> {
  const response = await api.get(`/api/v1/items/${itemId}/annotations`)
  expect(response.ok()).toBeTruthy()
  return (await response.json()) as AnnotationRow[]
}

async function me(api: APIRequestContext): Promise<string> {
  return ((await (await api.get('/api/v1/auth/me')).json()) as { id: string }).id
}

/** Claim from the annotate queue and wait for the canvas. */
async function claimFromQueue(page: Page, workflow: WorkflowProject): Promise<Locator> {
  await page.goto(`/projects/${workflow.id}/annotate`)
  await expect(page).toHaveURL(new RegExp(`/projects/${workflow.id}/annotate/${workflow.itemId}$`))
  return annotatorCanvas(page)
}

/** Draw one box on the claimed item and submit it; `shapes` is the count after drawing. */
async function drawAndSubmit(
  page: Page,
  canvas: Locator,
  workflow: WorkflowProject,
  shapes = 1,
): Promise<void> {
  await page.keyboard.press('b')
  await dragBox(page, canvas, [0.2, 0.2], [0.6, 0.6])
  await expect(page.getByRole('heading', { name: `Annotations (${shapes})` })).toBeVisible()

  const saved = page.waitForResponse(
    (response) =>
      response.url().includes(`/items/${workflow.itemId}/annotations`) &&
      response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: 'Submit' }).click()
  expect((await saved).ok()).toBeTruthy()

  // The queue held one task, so submitting lands on the empty state.
  await expect(page.getByText('Queue is empty', { exact: true })).toBeVisible()
  await expect(page).toHaveURL(new RegExp(`/projects/${workflow.id}/annotate$`))
}

test('the workflow settings round-trip through the settings page (WF-1)', async ({
  page,
  api,
  workflow,
}) => {
  await page.goto(`/projects/${workflow.id}/settings`)
  const section = page.getByRole('region', { name: 'Workflow' })
  await expect(section.getByLabel('review', { exact: true })).toHaveValue('required')

  await section.getByLabel('review', { exact: true }).selectOption('none')
  await section.getByLabel(/skip items/).uncheck()
  await section.getByRole('button', { name: 'Save workflow' }).click()
  await expect(section.getByRole('status')).toHaveText('Saved.')

  let project = (await (await api.get(`/api/v1/projects/${workflow.id}`)).json()) as {
    workflow: Record<string, unknown>
  }
  expect(project.workflow).toMatchObject({ review: 'none', allow_skip: false })

  // Put the default flow back: the rest of this file walks through review.
  await section.getByLabel('review', { exact: true }).selectOption('required')
  await section.getByLabel(/skip items/).check()
  await section.getByRole('button', { name: 'Save workflow' }).click()
  await expect(section.getByRole('status')).toHaveText('Saved.')
  project = (await (await api.get(`/api/v1/projects/${workflow.id}`)).json()) as {
    workflow: Record<string, unknown>
  }
  expect(project.workflow).toMatchObject({ review: 'required', allow_skip: true })
})

test('the scan opened one open annotate task and no review task', async ({ api, workflow }) => {
  const rows = await tasks(api, workflow.id)
  expect(rows).toHaveLength(1)
  expect(rows[0]).toMatchObject({
    item_id: workflow.itemId,
    type: 'annotate',
    status: 'open',
    locked_by_id: null,
  })
})

// Claim, release and submit live in one test on purpose: a claim leaves the
// task locked by this user, and a full-page navigation (as between tests)
// does not run the unmount release, so the next claim would find nothing.
test('claiming locks the task, releasing returns it, submitting opens a review task', async ({
  page,
  api,
  workflow,
}) => {
  await claimFromQueue(page, workflow)
  await expect(page.getByText(/^locked until/)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Release' })).toBeVisible()

  const [task] = await tasks(api, workflow.id)
  expect(task).toMatchObject({ type: 'annotate', status: 'in_progress', assignee_id: await me(api) })
  expect(task.locked_by_id).toBe(task.assignee_id)

  // Releasing hands the lock back and returns to the queue, which claims the
  // same task again — the only one there is.
  const released = page.waitForResponse(
    (response) =>
      response.url().includes(`/tasks/${task.id}/release`) && response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: 'Release' }).click()
  expect((await released).ok()).toBeTruthy()
  await expect(page).toHaveURL(new RegExp(`/projects/${workflow.id}/annotate/${workflow.itemId}$`))
  const canvas = await annotatorCanvas(page)
  await expect(page.getByText(/^locked until/)).toBeVisible()

  await drawAndSubmit(page, canvas, workflow)

  const [newest] = await versions(api, workflow.itemId)
  expect(newest).toMatchObject({ status: 'submitted', source: 'human' })
  expect(newest.result.shapes.some((shape) => shape.type === 'bbox')).toBe(true)

  const rows = await tasks(api, workflow.id)
  expect(rows.filter((row) => row.type === 'annotate').map((row) => row.status)).toEqual(['done'])
  expect(rows.filter((row) => row.type === 'review')).toMatchObject([
    { status: 'open', item_id: workflow.itemId },
  ])

  const item = (await (await api.get(`/api/v1/items/${workflow.itemId}`)).json()) as {
    status: string
  }
  expect(item.status).toBe('submitted')
})

test('rejecting in review sends the item back to the same annotator', async ({
  page,
  api,
  workflow,
}) => {
  await page.goto(`/projects/${workflow.id}/review`)
  await expect(page).toHaveURL(new RegExp(`/projects/${workflow.id}/review/${workflow.itemId}$`))
  await expect(page.getByRole('heading', { name: 'Review' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Versions' })).toBeVisible()

  // A rejection needs a comment; the button says so by staying disabled.
  const reject = page.getByRole('button', { name: 'Reject' })
  await expect(reject).toBeDisabled()
  await page.getByRole('textbox', { name: 'Comment', exact: true }).fill('Box is too loose around the car.')
  await expect(reject).toBeEnabled()

  const reviewed = page.waitForResponse(
    (response) => response.url().includes('/review') && response.request().method() === 'POST',
  )
  await reject.click()
  expect((await reviewed).ok()).toBeTruthy()
  await expect(page.getByText('Nothing to review', { exact: true })).toBeVisible()

  const [newest] = await versions(api, workflow.itemId)
  expect(newest.status).toBe('rejected')

  const rows = await tasks(api, workflow.id)
  expect(rows.filter((row) => row.type === 'review').map((row) => row.status)).toEqual(['done'])
  const reopened = rows.filter((row) => row.type === 'annotate' && row.status === 'open')
  expect(reopened).toHaveLength(1)
  expect(reopened[0].assignee_id).toBe(await me(api))

  // The reviewer's comment is on the item's thread.
  const comments = (await (await api.get(`/api/v1/items/${workflow.itemId}/comments`)).json()) as Array<{
    body: string
  }>
  expect(comments.some((comment) => comment.body.includes('too loose'))).toBe(true)
})

test('the rejected item comes back through the queue and can be approved', async ({
  page,
  api,
  workflow,
}) => {
  // The annotator reopens on the rejected version, so the fix builds on it.
  const canvas = await claimFromQueue(page, workflow)
  await expect(page.getByRole('heading', { name: 'Annotations (1)' })).toBeVisible()
  await drawAndSubmit(page, canvas, workflow, 2)

  await page.goto(`/projects/${workflow.id}/review`)
  await expect(page).toHaveURL(new RegExp(`/projects/${workflow.id}/review/${workflow.itemId}$`))

  const reviewed = page.waitForResponse(
    (response) => response.url().includes('/review') && response.request().method() === 'POST',
  )
  await page.getByRole('button', { name: 'Approve' }).click()
  expect((await reviewed).ok()).toBeTruthy()
  await expect(page.getByText('Nothing to review', { exact: true })).toBeVisible()

  const [newest] = await versions(api, workflow.itemId)
  expect(newest.status).toBe('approved')

  const rows = await tasks(api, workflow.id)
  expect(rows.every((row) => row.status === 'done')).toBe(true)
  expect(rows.filter((row) => row.type === 'annotate')).toHaveLength(2)
  expect(rows.filter((row) => row.type === 'review')).toHaveLength(2)

  const item = (await (await api.get(`/api/v1/items/${workflow.itemId}`)).json()) as {
    status: string
  }
  expect(item.status).toBe('approved')

  // Both queues are now empty for this project.
  await page.goto(`/projects/${workflow.id}/annotate`)
  await expect(page.getByText('Queue is empty', { exact: true })).toBeVisible()
})
