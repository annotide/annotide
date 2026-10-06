/**
 * Accessibility against WCAG 2.1 AA (UX-7).
 *
 * axe-core checks every page in both themes for what a machine can judge
 * (contrast, names, roles, landmarks, labels). What it cannot — that the
 * annotator works without a pointer (2.1.1), the skip link (2.4.1), titles
 * that follow the route (2.4.2), no sideways scrolling at 320 px (1.4.10) —
 * is driven here by hand. Any new violation
 * fails the suite, so a regression shows up in the change that caused it.
 */
import AxeBuilder from '@axe-core/playwright'
import type { Page } from '@playwright/test'

import { createScratchProject, expect, newApiContext, test as base } from './support'

const WCAG_AA = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa']

const test = base.extend<Record<never, never>, { keyboardProject: { id: string; itemId: string } }>({
  keyboardProject: [
    async ({ playwright }, use, workerInfo) => {
      const api = await newApiContext(playwright, workerInfo.project.use.baseURL)
      const project = await createScratchProject(api, 'E2E keyboard', '*/scene-001-*.jpg')
      try {
        await use({ id: project.id, itemId: project.itemIds[0] })
      } finally {
        await api.delete(`/api/v1/projects/${project.id}`)
        await api.dispose()
      }
    },
    { scope: 'worker' },
  ],
})

async function violations(page: Page): Promise<string[]> {
  const result = await new AxeBuilder({ page }).withTags(WCAG_AA).analyze()
  return result.violations.flatMap((violation) =>
    violation.nodes.map(
      (node) => `${violation.id}: ${node.target.join(' ')} — ${node.failureSummary ?? ''}`,
    ),
  )
}

for (const theme of ['dark', 'light'] as const) {
  test(`every page passes axe in the ${theme} theme`, async ({ page, demo }) => {
    test.setTimeout(120_000)
    await page.addInitScript((value) => {
      localStorage.setItem('annotation.ui', JSON.stringify({ theme: value, sidebarOpen: true }))
    }, theme)
    const routes = [
      '/',
      `/projects/${demo.id}`,
      `/projects/${demo.id}/dashboard`,
      `/projects/${demo.id}/settings`,
      `/projects/${demo.id}/annotate/${demo.firstItemId}`,
      `/projects/${demo.id}/review`,
      '/connectors',
      '/models',
      '/webhooks',
      '/settings/api-keys',
      '/settings/license',
      '/settings/security',
      '/settings/users',
      '/no-such-page',
    ]
    const found: Record<string, string[]> = {}
    for (const route of routes) {
      await page.goto(route)
      await page.waitForLoadState('networkidle')
      const list = await violations(page)
      if (list.length > 0) found[route] = list
    }
    expect(found).toEqual({})
  })
}

test('pages reflow to 320 px without scrolling sideways', async ({ page, demo }) => {
  test.setTimeout(120_000)
  await page.setViewportSize({ width: 320, height: 640 })
  // The annotate and review canvases are two-dimensional content, which
  // 1.4.10 exempts; data tables scroll inside their own container.
  const routes = [
    '/',
    `/projects/${demo.id}`,
    `/projects/${demo.id}/dashboard`,
    `/projects/${demo.id}/settings`,
    '/connectors',
    '/models',
    '/webhooks',
    '/settings/api-keys',
    '/settings/license',
    '/settings/security',
    '/settings/users',
  ]
  const overflowing: Record<string, number> = {}
  for (const route of routes) {
    await page.goto(route)
    await page.waitForLoadState('networkidle')
    const width = await page.evaluate(() => document.documentElement.scrollWidth)
    if (width > 320) overflowing[route] = width
  }
  expect(overflowing).toEqual({})
})

test('the sign-in page passes axe', async ({ browser }) => {
  const context = await browser.newContext({ storageState: { cookies: [], origins: [] } })
  try {
    const page = await context.newPage()
    await page.goto('/login')
    await page.waitForLoadState('networkidle')
    await expect(page).toHaveTitle('Sign in · Annotide')
    expect(await violations(page)).toEqual([])
  } finally {
    await context.close()
  }
})

test('the first Tab reaches a skip link that moves focus to the page', async ({ page }) => {
  await page.goto('/connectors')
  await expect(page).toHaveTitle('Connectors · Annotide')
  await expect(page.getByRole('heading', { name: 'Connectors', level: 1 })).toBeVisible()

  await page.keyboard.press('Tab')
  const skip = page.getByRole('link', { name: 'Skip to main content' })
  await expect(skip).toBeFocused()
  await page.keyboard.press('Enter')
  await expect(page.locator('main#main')).toBeFocused()
})

test('a box is drawn, selected and deleted without a pointer', async ({
  page,
  keyboardProject,
}) => {
  await page.goto(`/projects/${keyboardProject.id}/annotate/${keyboardProject.itemId}`)
  await expect(page).toHaveTitle(/^Annotate · E2E keyboard .* · Annotide$/)
  const shapes = page.getByRole('list', { name: /^Annotations \(\d+\)$/ })
  await expect(page.getByText('No shapes yet.')).toBeVisible()

  // The annotator is lazy-loaded; keys pressed before it mounts go nowhere.
  const selectTool = page.getByRole('button', { name: /^Select/, pressed: true })
  await expect(selectTool).toBeVisible()
  await page.keyboard.press('b')
  await expect(page.getByRole('button', { name: /^Box/, pressed: true })).toBeVisible()
  await page.keyboard.press('ArrowRight') // the crosshair appears
  await expect(page.getByText(/^Keyboard cursor at \d+, \d+/)).toBeVisible()
  await page.keyboard.press(' ') // first corner
  for (let i = 0; i < 4; i++) await page.keyboard.press('Shift+ArrowRight')
  for (let i = 0; i < 3; i++) await page.keyboard.press('Shift+ArrowDown')
  await page.keyboard.press(' ') // opposite corner

  // A new box selects itself.
  const entry = shapes.getByRole('button')
  await expect(entry).toHaveCount(1)
  await expect(entry).toHaveAttribute('aria-pressed', 'true')

  // Deselected and selected again from the shape list, then deleted.
  await entry.focus()
  await page.keyboard.press('Enter')
  await expect(entry).toHaveAttribute('aria-pressed', 'false')
  await page.keyboard.press('Enter')
  await expect(entry).toHaveAttribute('aria-pressed', 'true')
  await page.keyboard.press('v')
  await page.keyboard.press('Delete')
  await expect(page.getByText('No shapes yet.')).toBeVisible()
})
