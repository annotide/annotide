/**
 * Project list, creation, settings and deletion — the setup loop an owner
 * goes through from the browser. Everything created here is deleted here.
 */
import { DEMO_PROJECT_NAME, expect, test } from './support'

test('the seeded demo project is listed and opens to its item grid', async ({ page, demo }) => {
  await page.goto('/')
  await page.getByRole('link', { name: DEMO_PROJECT_NAME }).click()

  await expect(page).toHaveURL(new RegExp(`/projects/${demo.id}$`))
  await expect(page.getByRole('heading', { name: DEMO_PROJECT_NAME })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Start annotating' })).toBeVisible()

  // The seed scanned real objects, so the grid links every item to the annotator.
  const itemLinks = page.locator(`a[href^="/projects/${demo.id}/annotate/"]`)
  await expect(itemLinks.first()).toBeVisible()
  await expect(itemLinks.first()).toHaveAttribute('href', /\/annotate\/[0-9a-f-]{36}$/)
})

test('a project can be created, edited in settings and deleted again', async ({ page, api }) => {
  const name = `E2E project ${Date.now()}`

  await page.goto('/')
  await page.getByRole('button', { name: 'New project' }).click()

  const form = page.getByRole('form', { name: 'New project' })
  await form.getByLabel('Name').fill(name)
  await form.getByLabel('Description').fill('created by the Playwright suite')
  await form.getByRole('button', { name: 'Create project' }).click()

  // Creation hands over to the settings page for connectors and the schema.
  await expect(page).toHaveURL(/\/projects\/[0-9a-f-]{36}\/settings$/)
  await expect(page.getByRole('heading', { name: `${name} settings` })).toBeVisible()
  const projectId = page.url().match(/\/projects\/([0-9a-f-]{36})\/settings$/)?.[1]
  expect(projectId).toBeTruthy()

  // The creator is an owner (they see the danger zone) and the API agrees.
  await expect(page.getByRole('heading', { name: 'Danger zone' })).toBeVisible()
  const detail = await api.get(`/api/v1/projects/${projectId}`)
  expect(detail.ok()).toBeTruthy()
  expect(((await detail.json()) as { name: string }).name).toBe(name)

  // Delete through the two-step confirmation and land back on the list.
  await page.getByRole('button', { name: 'Delete project' }).click()
  await page.getByRole('button', { name: 'Confirm delete' }).click()
  await expect(page).toHaveURL(/\/$/)
  await expect(page.getByRole('heading', { name: 'Projects' })).toBeVisible()
  await expect(page.getByRole('link', { name })).toHaveCount(0)

  const gone = await api.get(`/api/v1/projects/${projectId}`)
  expect(gone.status()).toBe(404)
})

test('the dashboard renders the summary for the demo project', async ({ page, demo }) => {
  await page.goto(`/projects/${demo.id}/dashboard`)

  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible()
  await expect(page.getByRole('region', { name: /summary/i })).toBeVisible()
  await expect(page.getByTestId('throughput')).toBeVisible()
  await expect(page.getByLabel('Throughput window')).toBeVisible()
})
