/**
 * Image + caption (§5 multimodal) on the seeded "Demo: image and caption"
 * project: the caption, a companion view found by the scan, shows beside the
 * photo, and the judgement is saved as the item's classification.
 */
import { expect, test } from './support'

const PROJECT = 'Demo: image and caption'

test('shows the caption beside the photo and saves the judgement', async ({ page, api }) => {
  const projects = await api.get('/api/v1/projects', { params: { limit: 100 } })
  const { items } = (await projects.json()) as { items: Array<{ id: string; name: string }> }
  const project = items.find((candidate) => candidate.name === PROJECT)
  expect(project, `"${PROJECT}" not found — run \`make seed\``).toBeTruthy()
  const itemPage = await api.get(`/api/v1/projects/${project!.id}/items`, {
    params: { q: '03.jpg', limit: 1 },
  })
  const item = ((await itemPage.json()) as { items: Array<{ id: string }> }).items[0]
  expect(item, 'the caption files must not have become items').toBeTruthy()
  const before = ((await (await api.get(`/api/v1/items/${item!.id}/annotations`)).json()) as unknown[])
    .length

  await page.goto(`/projects/${project!.id}/annotate/${item!.id}`)
  const context = page.getByRole('region', { name: 'Context' })
  await expect(context.getByText('A quiet beach at sunset with no people.')).toBeVisible()

  // An untouched box is "no answer yet"; checking and clearing it records false.
  const matches = page.getByRole('checkbox', { name: /caption_matches/ })
  await matches.check()
  await matches.uncheck()
  await page.getByLabel(/problem/).selectOption('wrong place')
  await page.getByRole('button', { name: /Save draft/ }).click()

  await expect
    .poll(async () => ((await (await api.get(`/api/v1/items/${item!.id}/annotations`)).json()) as unknown[]).length)
    .toBeGreaterThan(before)
  const [saved] = (await (await api.get(`/api/v1/items/${item!.id}/annotations`)).json()) as Array<{
    result: { classification: Record<string, unknown> }
  }>
  expect(saved!.result.classification).toMatchObject({
    caption_matches: false,
    problem: 'wrong place',
  })
})
