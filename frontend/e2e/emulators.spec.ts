/**
 * The annotator on the AWS and Google Cloud emulators: the browser fetches
 * each item straight from Moto (S3, a SigV4-signed URL) and fake-gcs-server
 * (GCS), past the CSP and the bucket's CORS rule (ARC-3, SRC-7). Opt-in: needs
 * `make seed-emulators` against the stack under test, then
 * `E2E_EMULATORS=1 npx playwright test emulators`.
 */
import { annotatorCanvas, expect, test } from './support'

test.skip(!process.env.E2E_EMULATORS, 'set E2E_EMULATORS=1 after `make seed-emulators`')

const PROJECTS = [
  { name: 'Demo on S3: traffic objects', origin: 'http://localhost:9100' },
  { name: 'Demo on GCS: traffic objects', origin: 'http://localhost:4443' },
]

for (const { name, origin } of PROJECTS) {
  test(`${name}: the item loads from the emulator`, async ({ page, api }) => {
    const projects = await api.get('/api/v1/projects', { params: { limit: 100 } })
    const { items } = (await projects.json()) as { items: Array<{ id: string; name: string }> }
    const project = items.find((candidate) => candidate.name === name)
    expect(project, `"${name}" not found — run \`make seed-emulators\``).toBeTruthy()
    const page1 = await api.get(`/api/v1/projects/${project!.id}/items`, { params: { limit: 1 } })
    const item = ((await page1.json()) as { items: Array<{ id: string }> }).items[0]
    expect(item, `"${name}" has no items`).toBeTruthy()

    const media = page.waitForResponse((response) => response.url().startsWith(`${origin}/`))
    await page.goto(`/projects/${project!.id}/annotate/${item!.id}`)

    const response = await media
    expect(response.status()).toBe(200)
    expect(response.headers()['content-type']).toBe('image/jpeg')
    await annotatorCanvas(page)
  })
}
