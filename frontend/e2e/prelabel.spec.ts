/**
 * Pre-labelling end to end (ML-2, ML-10, BYOM-7): the Pre-label panel queues
 * a `prelabel` job, the compose `worker` signs a URL for the item, the
 * compose `model` service fetches the image and predicts, and the worker maps
 * the model's `object` class onto the project's `car` and stores a draft.
 * Nothing is faked: this is the path the backend tests cover only with a
 * stub model client.
 *
 * Works on a scratch project (one sample image) so the drafts it writes do
 * not reach the demo project; hard-deleted at the end.
 */
import type { APIRequestContext, Page } from '@playwright/test'

import {
  annotatorCanvas,
  createScratchProject,
  DEMO_MODEL_NAME,
  expect,
  newApiContext,
  test as base,
  type ScratchProject,
  waitForJob,
} from './support'

// The heuristic model finds boxes in scene-001 (scene-002 comes back empty).
const SAMPLE_GLOB = '*/scene-001-*.jpg'

interface JobRow {
  id: string
  status: string
  error: string | null
  result: Record<string, unknown> | null
}

interface AnnotationRow {
  source: 'human' | 'model'
  status: string
  author_model_version_id: string | null
  result: { shapes: Array<{ type: string; class: string; confidence?: number | null }> }
}

const test = base.extend<Record<never, never>, { scratch: ScratchProject }>({
  scratch: [
    async ({ playwright }, use, workerInfo) => {
      const api = await newApiContext(playwright, workerInfo.project.use.baseURL)
      const project = await createScratchProject(
        api,
        `E2E prelabel ${workerInfo.workerIndex}`,
        SAMPLE_GLOB,
      )
      try {
        expect(project.itemIds, `glob ${SAMPLE_GLOB} should match exactly one sample`).toHaveLength(1)
        await use(project)
      } finally {
        await api.delete(`/api/v1/projects/${project.id}`)
        await api.dispose()
      }
    },
    { scope: 'worker' },
  ],
})

// The second test re-runs the first one's model version.
test.describe.configure({ mode: 'serial' })

async function demoModelVersionId(api: APIRequestContext): Promise<string> {
  const models = (await (await api.get('/api/v1/models', { params: { limit: 100 } })).json()) as {
    items: Array<{ id: string; name: string }>
  }
  const model = models.items.find((candidate) => candidate.name === DEMO_MODEL_NAME)
  if (!model) throw new Error(`"${DEMO_MODEL_NAME}" not registered — run \`make seed\`.`)
  const versions = (await (await api.get(`/api/v1/models/${model.id}/versions`)).json()) as {
    items: Array<{ id: string; version: number }>
  }
  return versions.items.reduce((a, b) => (b.version > a.version ? b : a)).id
}

/** Start a pre-label run from the panel; resolve once its job has settled. */
async function runFromPanel(page: Page, api: APIRequestContext, projectId: string): Promise<JobRow> {
  await page.goto(`/projects/${projectId}?tab=prelabel`)
  const panel = page.locator('section', { has: page.getByRole('heading', { name: 'Pre-label' }) })
  await panel.getByLabel('Model').selectOption({ label: DEMO_MODEL_NAME })

  const queued = page.waitForResponse(
    (response) =>
      response.url().endsWith(`/projects/${projectId}/prelabel`) &&
      response.request().method() === 'POST',
  )
  await panel.getByRole('button', { name: 'Pre-label', exact: true }).click()
  const response = await queued
  expect(response.status(), await response.text()).toBe(202)
  const { id } = (await response.json()) as { id: string }

  await waitForJob(api, id)
  // The panel polls while a job is in flight and lists the newest first.
  await expect(panel.getByRole('listitem').first()).toContainText('succeeded · 100%')
  return (await (await api.get(`/api/v1/jobs/${id}`)).json()) as JobRow
}

test('the compose model pre-labels the item with a mapped draft (ML-2)', async ({
  page,
  api,
  scratch,
}) => {
  const versionId = await demoModelVersionId(api)
  const job = await runFromPanel(page, api, scratch.id)

  expect(job.result).toMatchObject({
    model_version_id: versionId,
    selected: 1,
    predicted: 1,
    errors: 0,
    skipped_human: 0,
  })
  await expect(page.getByText('1/1 predicted, 0 empty, 0 skipped (human), 0 errors')).toBeVisible()

  const [itemId] = scratch.itemIds
  const annotations = (await (
    await api.get(`/api/v1/items/${itemId}/annotations`)
  ).json()) as AnnotationRow[]
  expect(annotations).toHaveLength(1)
  const [draft] = annotations
  expect(draft).toMatchObject({
    source: 'model',
    status: 'draft',
    author_model_version_id: versionId,
  })
  expect(draft.result.shapes.length).toBeGreaterThan(0)
  // BYOM-2: the model only knows `object`; the seed maps it onto `car`.
  for (const shape of draft.result.shapes) {
    expect(shape).toMatchObject({ type: 'bbox', class: 'car' })
    expect(shape.confidence ?? 0).toBeGreaterThan(0)
  }

  const item = (await (await api.get(`/api/v1/items/${itemId}`)).json()) as { status: string }
  expect(item.status).toBe('prelabeled')

  // The annotator opens on the model's draft.
  await page.goto(`/projects/${scratch.id}/annotate/${itemId}`)
  await annotatorCanvas(page)
  await expect(
    page.getByRole('heading', { name: `Annotations (${draft.result.shapes.length})` }),
  ).toBeVisible()
})

test('re-running the same model version is a no-op (ML-10)', async ({ page, api, scratch }) => {
  const job = await runFromPanel(page, api, scratch.id)
  expect(job.result).toMatchObject({ selected: 0, predicted: 0, skipped_done: 1 })

  const annotations = (await (
    await api.get(`/api/v1/items/${scratch.itemIds[0]}/annotations`)
  ).json()) as AnnotationRow[]
  expect(annotations).toHaveLength(1)
})
