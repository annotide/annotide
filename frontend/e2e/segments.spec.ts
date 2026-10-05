/**
 * Audio and time series (§5) on the seeded "Demo: audio and time series"
 * project: a segment dragged across the waveform (the audio decodes from its
 * signed URL), a speaker typed in, and an interval on the sensor channels.
 */
import type { APIRequestContext, Page } from '@playwright/test'

import { expect, test } from './support'

const PROJECT = 'Demo: audio and time series'

interface Row {
  result: { media_type: string; shapes: Array<Record<string, unknown>> }
}

async function itemByName(api: APIRequestContext, q: string): Promise<[string, string]> {
  const projects = await api.get('/api/v1/projects', { params: { limit: 100 } })
  const { items } = (await projects.json()) as { items: Array<{ id: string; name: string }> }
  const project = items.find((candidate) => candidate.name === PROJECT)
  expect(project, `"${PROJECT}" not found — run \`make seed\``).toBeTruthy()
  const page = await api.get(`/api/v1/projects/${project!.id}/items`, { params: { q, limit: 1 } })
  const item = ((await page.json()) as { items: Array<{ id: string }> }).items[0]
  expect(item).toBeTruthy()
  return [project!.id, item!.id]
}

async function latest(api: APIRequestContext, itemId: string, count: number): Promise<Row> {
  await expect
    .poll(async () => ((await (await api.get(`/api/v1/items/${itemId}/annotations`)).json()) as Row[]).length)
    .toBeGreaterThan(count)
  const [row] = (await (await api.get(`/api/v1/items/${itemId}/annotations`)).json()) as Row[]
  return row!
}

/** Earlier runs leave drafts on the seeded items: start from no segments. */
async function clearSegments(page: Page): Promise<void> {
  const remove = page.getByRole('button', { name: /^Remove Segment/ })
  while ((await remove.count()) > 0) await remove.first().click()
}

async function versions(api: APIRequestContext, itemId: string): Promise<number> {
  return ((await (await api.get(`/api/v1/items/${itemId}/annotations`)).json()) as Row[]).length
}

test('marks a segment on the waveform and names its speaker', async ({ page, api }) => {
  const [projectId, itemId] = await itemByName(api, 'tones')
  const before = await versions(api, itemId)

  await page.goto(`/projects/${projectId}/annotate/${itemId}`)
  await expect(page.getByText('0:00.000 / 0:06.000')).toBeVisible()
  await clearSegments(page)
  const timeline = page.getByRole('group', { name: /Timeline/ })
  const box = (await timeline.boundingBox())!
  // About the first second: the 440 Hz tone.
  await page.mouse.move(box.x + 2, box.y + box.height / 2)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width / 6, box.y + box.height / 2, { steps: 5 })
  await page.mouse.up()
  await page.getByLabel('Segment 1: Speaker').fill('Robot')
  await page.getByRole('button', { name: /Save draft/ }).click()

  const saved = await latest(api, itemId, before)
  expect(saved.result.media_type).toBe('audio')
  expect(saved.result.shapes).toHaveLength(1)
  const [segment] = saved.result.shapes
  expect(segment).toMatchObject({ type: 'segment', class: 'tone', speaker: 'Robot' })
  expect(Number(segment!.end) - Number(segment!.start)).toBeGreaterThan(800)
  expect(Number.isInteger(segment!.start)).toBe(true)
})

test('marks an interval on two of three sensor channels', async ({ page, api }) => {
  const [projectId, itemId] = await itemByName(api, 'pump-7')
  const before = await versions(api, itemId)

  await page.goto(`/projects/${projectId}/annotate/${itemId}`)
  await expect(page.getByText('seconds: From 0 to 199')).toBeVisible()
  await clearSegments(page)
  await page.getByLabel('New segments:').selectOption('anomaly')
  await page.getByRole('button', { name: 'Add segment for the selection' }).click()
  const list = page.getByRole('list', { name: 'Segments' })
  await list.getByRole('checkbox', { name: 'temperature' }).uncheck()
  // The keyboard path: a start past the end moves the segment, and the end
  // field Tab lands on must still be replaced by typing, not appended to.
  await page.getByLabel('Segment 1: Start').fill('120')
  await page.keyboard.press('Tab')
  await expect(page.getByLabel('Segment 1: End')).toBeFocused()
  await page.keyboard.type('135')
  await page.keyboard.press('Enter')
  await expect(page.getByLabel('Segment 1: End')).toHaveValue('135')
  await page.getByRole('button', { name: /Save draft/ }).click()

  const saved = await latest(api, itemId, before)
  expect(saved.result.media_type).toBe('timeseries')
  expect(saved.result.shapes).toHaveLength(1)
  expect(saved.result.shapes[0]).toMatchObject({
    type: 'segment',
    class: 'anomaly',
    start: 120,
    end: 135,
    channels: ['vibration', 'pressure'],
  })
})
