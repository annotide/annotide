/**
 * LLM evaluation (§5 LLM-data) on the seeded "Demo: LLM evaluation" project:
 * the annotator shows the conversation and both responses, a preference and
 * a rating are recorded, and the draft carries `ranking` / `rating` shapes.
 */
import { expect, test } from './support'

const PROJECT = 'Demo: LLM evaluation'

interface Row {
  version: number
  result: { media_type: string; shapes: Array<Record<string, unknown>> }
}

test('ranks two responses and rates one', async ({ page, api }) => {
  const projects = await api.get('/api/v1/projects', { params: { limit: 100 } })
  const { items } = (await projects.json()) as { items: Array<{ id: string; name: string }> }
  const project = items.find((candidate) => candidate.name === PROJECT)
  expect(project, `"${PROJECT}" not found — run \`make seed\``).toBeTruthy()
  const itemPage = await api.get(`/api/v1/projects/${project!.id}/items`, {
    params: { q: '01-preference', limit: 1 },
  })
  const item = ((await itemPage.json()) as { items: Array<{ id: string }> }).items[0]
  expect(item).toBeTruthy()
  const before = ((await (await api.get(`/api/v1/items/${item!.id}/annotations`)).json()) as Row[])
    .length

  await page.goto(`/projects/${project!.id}/annotate/${item!.id}`)
  await expect(page.getByText('What is the capital of Finland?')).toBeVisible()
  await expect(page.getByText('The capital of Finland is Turku.')).toBeVisible()

  await page.getByRole('button', { name: 'A is better' }).click()
  await page
    .getByRole('group', { name: 'Response A: Helpfulness' })
    .getByLabel('Excellent')
    .check()
  await page.getByRole('button', { name: /Save draft/ }).click()

  await expect
    .poll(async () => {
      const rows = (await (await api.get(`/api/v1/items/${item!.id}/annotations`)).json()) as Row[]
      return rows.length
    })
    .toBeGreaterThan(before)
  const [saved] = (await (await api.get(`/api/v1/items/${item!.id}/annotations`)).json()) as Row[]
  expect(saved.result.media_type).toBe('llm')
  expect(saved.result.shapes).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ type: 'ranking', class: 'preference', order: ['a', 'b'] }),
      expect.objectContaining({
        type: 'rating',
        class: 'helpfulness',
        target: 'response:a',
        value: 5,
      }),
    ]),
  )
})
