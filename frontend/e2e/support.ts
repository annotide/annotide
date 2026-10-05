/**
 * Shared fixtures for the E2E suite.
 *
 * `test` extends Playwright's with:
 * - `api`     an APIRequestContext carrying the seeded user's bearer token,
 *             for setup/assertions that go around the UI
 * - `demo`    the project `make seed` creates, plus its first item
 *
 * The suite never creates fixtures behind the UI's back beyond reading them:
 * anything a test creates through the UI it also deletes through the UI.
 */
import { readFileSync } from 'node:fs'
import { inflateRawSync } from 'node:zlib'

import {
  expect,
  test as base,
  type APIRequestContext,
  type Locator,
  type Page,
  type PlaywrightWorkerArgs,
} from '@playwright/test'

export const credentials = {
  email: process.env.E2E_EMAIL ?? 'admin@example.com',
  password: process.env.E2E_PASSWORD ?? 'admin-dev-password',
}

/** Name of the project `python -m app.demo seed` creates (backend/app/demo.py). */
export const DEMO_PROJECT_NAME = 'Demo: traffic objects'

export const STORAGE_STATE = 'e2e/.auth/admin.json'

/** `backend/app/demo.py::DEMO_MODEL_NAME` — the compose model service, `detect`. */
export const DEMO_MODEL_NAME = 'Reference model (compose)'

export interface DemoProject {
  id: string
  name: string
  firstItemId: string
}

interface Fixtures {
  api: APIRequestContext
  demo: DemoProject
}

/**
 * The token `auth.setup.ts` saved with the storage state, if it is there.
 * Reusing it keeps the suite to one login however many tests run: logins
 * are rate limited per (IP, e-mail) (API-5), and a login per test would
 * trip it.
 */
function savedToken(): string | null {
  try {
    const state = JSON.parse(readFileSync(STORAGE_STATE, 'utf8')) as {
      origins?: Array<{ localStorage: Array<{ name: string; value: string }> }>
    }
    for (const origin of state.origins ?? []) {
      const entry = origin.localStorage.find((item) => item.name === 'annotation.auth')
      const token = entry ? (JSON.parse(entry.value) as { token?: string }).token : undefined
      if (token) return token
    }
  } catch {
    // No storage state yet (e.g. running auth.setup itself): log in instead.
  }
  return null
}

let cachedToken: string | null = null

/**
 * A request context carrying the seeded user's bearer token. Shared by the
 * test-scoped `api` fixture and worker-scoped fixtures (which cannot depend
 * on `api` directly). Logs in through the API only when no saved token is
 * available, and then once per worker.
 */
export async function newApiContext(
  playwright: PlaywrightWorkerArgs['playwright'],
  baseURL: string | undefined,
): Promise<APIRequestContext> {
  cachedToken ??= savedToken()
  if (!cachedToken) {
    const anonymous = await playwright.request.newContext({ baseURL })
    const login = await anonymous.post('/api/v1/auth/login', { data: credentials })
    expect(login.ok(), `login as ${credentials.email} failed: ${login.status()}`).toBeTruthy()
    cachedToken = ((await login.json()) as { access_token: string }).access_token
    await anonymous.dispose()
  }
  const access_token = cachedToken

  return playwright.request.newContext({
    baseURL,
    extraHTTPHeaders: { Authorization: `Bearer ${access_token}` },
  })
}

export const test = base.extend<Fixtures>({
  api: async ({ playwright, baseURL }, use) => {
    const authed = await newApiContext(playwright, baseURL)
    await use(authed)
    await authed.dispose()
  },

  demo: async ({ api }, use) => {
    const projects = await api.get('/api/v1/projects', { params: { limit: 100 } })
    expect(projects.ok()).toBeTruthy()
    const { items } = (await projects.json()) as { items: Array<{ id: string; name: string }> }
    const project = items.find((candidate) => candidate.name === DEMO_PROJECT_NAME)
    if (!project) {
      throw new Error(
        `Project "${DEMO_PROJECT_NAME}" not found — run \`make seed\` against the stack under test.`,
      )
    }

    const itemPage = await api.get(`/api/v1/projects/${project.id}/items`, {
      params: { limit: 1 },
    })
    expect(itemPage.ok()).toBeTruthy()
    const firstItem = ((await itemPage.json()) as { items: Array<{ id: string }> }).items[0]
    if (!firstItem) {
      throw new Error(`Project "${DEMO_PROJECT_NAME}" has no items — did the seed scan run?`)
    }

    await use({ id: project.id, name: project.name, firstItemId: firstItem.id })
  },
})

export { expect }

/** Drive the login form. Resolves once the project list has rendered. */
export async function signIn(page: Page, creds = credentials): Promise<void> {
  await page.goto('/login')
  await page.getByLabel('Email').fill(creds.email)
  await page.getByLabel('Password').fill(creds.password)
  await page.getByRole('button', { name: 'Sign in' }).click()
  await expect(page.getByRole('heading', { name: 'Projects' })).toBeVisible()
}

/**
 * Wait until the annotator on the current page has its media and return the
 * Konva canvas. The stage only carries image coordinates once the image has
 * arrived, so drawing before this resolves would land nowhere.
 */
export async function annotatorCanvas(page: Page): Promise<Locator> {
  await expect(page.getByRole('toolbar', { name: 'Annotation tools' })).toBeVisible()
  const loading = page.getByRole('status', { name: '' }).filter({ hasText: 'Loading image' })
  await expect(loading).toHaveCount(0)
  await expect(page.getByRole('alert').filter({ hasText: 'could not be loaded' })).toHaveCount(0)

  const canvas = page.locator('.konvajs-content canvas').first()
  await expect(canvas).toBeVisible()
  return canvas
}

/** Drag a rectangle across the canvas, as fractions of its size. */
export async function dragBox(
  page: Page,
  canvas: Locator,
  from: [number, number],
  to: [number, number],
): Promise<void> {
  const box = await canvas.boundingBox()
  if (!box) throw new Error('canvas has no bounding box')
  const at = ([fx, fy]: [number, number]) => ({ x: box.x + box.width * fx, y: box.y + box.height * fy })
  const start = at(from)
  const end = at(to)
  await page.mouse.move(start.x, start.y)
  await page.mouse.down()
  await page.mouse.move((start.x + end.x) / 2, (start.y + end.y) / 2, { steps: 4 })
  await page.mouse.move(end.x, end.y, { steps: 4 })
  await page.mouse.up()
}

/** Click a sequence of points on the canvas, as fractions of its size. */
export async function clickPoints(
  page: Page,
  canvas: Locator,
  points: Array<[number, number]>,
): Promise<void> {
  const box = await canvas.boundingBox()
  if (!box) throw new Error('canvas has no bounding box')
  for (const [fx, fy] of points) {
    const x = box.x + box.width * fx
    const y = box.y + box.height * fy
    // Playwright counts rapid successive clicks as double-clicks, which would
    // finish a polygon or polyline early; pin every click to a single one.
    await page.mouse.click(x, y, { clickCount: 1 })
  }
}

/** Poll a job until it leaves `queued`/`running`; fail unless it succeeded. */
export async function waitForJob(api: APIRequestContext, jobId: string): Promise<void> {
  await expect
    .poll(
      async () => {
        const job = await api.get(`/api/v1/jobs/${jobId}`)
        return ((await job.json()) as { status: string }).status
      },
      { timeout: 60_000, message: `job ${jobId} did not finish` },
    )
    .toMatch(/^(succeeded|failed|cancelled)$/)
  const job = await api.get(`/api/v1/jobs/${jobId}`)
  const { status, error } = (await job.json()) as { status: string; error: string | null }
  expect(status, `job ${jobId} ended ${status}: ${error ?? ''}`).toBe('succeeded')
}

export interface ScratchProject {
  id: string
  itemIds: string[]
}

/**
 * Create a project of its own for a spec whose state must not leak into the
 * demo project: the demo's connectors, prefix and latest label schema, but a
 * `source_glob` that admits only `glob`. Scanned through the API (the compose
 * `worker` must be running). Hard-delete it with `api.delete` when done.
 * `prefix` replaces the demo's source prefix — for specs that upload their
 * own files, which must stay out of the demo's `samples/`.
 */
export async function createScratchProject(
  api: APIRequestContext,
  name: string,
  glob: string,
  prefix?: string,
): Promise<ScratchProject> {
  const list = await api.get('/api/v1/projects', { params: { limit: 100 } })
  const { items: projects } = (await list.json()) as {
    items: Array<{ id: string; name: string }>
  }
  const demoId = projects.find((project) => project.name === DEMO_PROJECT_NAME)?.id
  if (!demoId) throw new Error(`"${DEMO_PROJECT_NAME}" not found — run \`make seed\`.`)
  const demo = (await (await api.get(`/api/v1/projects/${demoId}`)).json()) as {
    source_connector_id: string
    result_connector_id: string
    source_prefix: string | null
  }
  const schemas = (await (await api.get(`/api/v1/projects/${demoId}/schemas`)).json()) as Array<{
    version: number
    definition: Record<string, unknown>
  }>
  const latest = schemas.reduce((a, b) => (b.version > a.version ? b : a))

  const created = await api.post('/api/v1/projects', {
    data: {
      name: `${name} ${Date.now()}`,
      description: 'created by the Playwright suite; safe to delete',
      source_connector_id: demo.source_connector_id,
      result_connector_id: demo.result_connector_id,
      source_prefix: prefix ?? demo.source_prefix,
      source_glob: glob,
    },
  })
  expect(created.status(), await created.text()).toBe(201)
  const { id } = (await created.json()) as { id: string }

  try {
    const schema = await api.post(`/api/v1/projects/${id}/schemas`, {
      data: { ...latest.definition, version: 1 },
    })
    expect(schema.status(), await schema.text()).toBe(201)

    const scan = await api.post(`/api/v1/projects/${id}/scan`, { data: {} })
    expect(scan.status(), await scan.text()).toBe(202)
    await waitForJob(api, ((await scan.json()) as { id: string }).id)

    const items = (await (await api.get(`/api/v1/projects/${id}/items`)).json()) as {
      items: Array<{ id: string }>
    }
    return { id, itemIds: items.items.map((item) => item.id) }
  } catch (error) {
    await api.delete(`/api/v1/projects/${id}`)
    throw error
  }
}

/**
 * One entry of a zip archive (an export bundle), read through the central
 * directory with `node:zlib`: stored or deflated entries, no zip64. Enough
 * for the exports the worker writes, without a zip dependency.
 */
export function readZipEntry(archive: Buffer, name: string): Buffer {
  const eocd = archive.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]))
  if (eocd < 0) throw new Error('not a zip archive')
  const entries = archive.readUInt16LE(eocd + 10)
  let at = archive.readUInt32LE(eocd + 16)
  const names: string[] = []
  for (let index = 0; index < entries; index += 1) {
    const method = archive.readUInt16LE(at + 10)
    const size = archive.readUInt32LE(at + 20)
    const nameLength = archive.readUInt16LE(at + 28)
    const skip = archive.readUInt16LE(at + 30) + archive.readUInt16LE(at + 32)
    const local = archive.readUInt32LE(at + 42)
    const entryName = archive.toString('utf8', at + 46, at + 46 + nameLength)
    names.push(entryName)
    if (entryName === name) {
      const start = local + 30 + archive.readUInt16LE(local + 26) + archive.readUInt16LE(local + 28)
      const data = archive.subarray(start, start + size)
      if (method === 0) return data
      if (method === 8) return inflateRawSync(data)
      throw new Error(`zip entry ${name} uses unsupported method ${method}`)
    }
    at += 46 + nameLength + skip
  }
  throw new Error(`zip has no ${name}; it has ${names.join(', ')}`)
}

/**
 * A two-page, 600 × 800 pt PDF with a real text layer (Helvetica). Page 2
 * says "Invoice total 42,00" with its baseline at y = 700 (PDF space), i.e.
 * 100 pt below the top edge.
 */
export function twoPagePdf(): Buffer {
  const pageText = ['Cover page', 'Invoice total 42,00']
  const objects: string[] = []
  objects[1] = '<< /Type /Catalog /Pages 2 0 R >>'
  objects[2] = '<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>'
  objects[5] = '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'
  pageText.forEach((text, index) => {
    const stream = `BT /F1 12 Tf 100 700 Td (${text}) Tj ET`
    objects[6 + index] = `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`
    objects[3 + index] =
      `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] ` +
      `/Resources << /Font << /F1 5 0 R >> >> /Contents ${6 + index} 0 R >>`
  })
  let body = '%PDF-1.4\n'
  const offsets: number[] = []
  for (let n = 1; n < objects.length; n++) {
    offsets[n] = Buffer.byteLength(body, 'latin1')
    body += `${n} 0 obj\n${objects[n]}\nendobj\n`
  }
  const xref = Buffer.byteLength(body, 'latin1')
  body += `xref\n0 ${objects.length}\n0000000000 65535 f \n`
  for (let n = 1; n < objects.length; n++) body += `${String(offsets[n]).padStart(10, '0')} 00000 n \n`
  body += `trailer\n<< /Size ${objects.length} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`
  return Buffer.from(body, 'latin1')
}
