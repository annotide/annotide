/**
 * The superuser's admin pages against the real stack: connectors (the demo
 * Azurite connector answers its check; a new connector is created, takes
 * storage events without a session until they are turned off, is edited
 * and deleted), models (the compose model service answers its check, its
 * versions list), organisation webhooks (created with a one-time secret,
 * paused, resumed, deleted), the user directory and the licence page.
 *
 * Anything a test creates it deletes through the UI; the `finally` blocks
 * only sweep up after a failure half-way.
 */
import type { APIRequestContext, Page } from '@playwright/test'

import { DEMO_MODEL_NAME, credentials, expect, test } from './support'

/** `backend/app/demo.py`: the one connector the seed registers, source and result. */
const DEMO_CONNECTOR_NAME = 'Azurite (demo)'

async function sweep(
  api: APIRequestContext,
  path: '/api/v1/connectors' | '/api/v1/webhooks',
  matches: (row: Record<string, unknown>) => boolean,
): Promise<void> {
  const list = (await (await api.get(path, { params: { limit: 100 } })).json()) as {
    items: Array<Record<string, unknown> & { id: string }>
  }
  for (const row of list.items.filter(matches)) await api.delete(`${path}/${row.id}`)
}

/** The `OK` / `Failed` line a Check button leaves on the page. */
function checkResult(page: Page) {
  return page.getByText(/^(OK|Failed)( — .*)?$/)
}

test('connectors: the demo connector checks out, and a new one is created, edited and deleted', async ({
  page,
  api,
  playwright,
}) => {
  const name = `E2E connector ${Date.now()}`
  try {
    await page.goto('/connectors')
    await expect(page.getByRole('heading', { name: 'Connectors', level: 1 })).toBeVisible()

    const demoRow = page.getByRole('row').filter({ hasText: DEMO_CONNECTOR_NAME })
    await demoRow.getByRole('button', { name: 'Check' }).click()
    await expect(checkResult(page)).toHaveText(/^OK/)

    const create = page.getByRole('form', { name: 'New connector' })
    await create.getByLabel('Name').fill(name)
    await create.getByLabel('Type').selectOption('local')
    await create.getByLabel('Config (JSON)').fill('{"root": "/tmp/e2e-connector"}')
    await create.getByRole('button', { name: 'Create connector' }).click()

    const row = page.getByRole('row').filter({ hasText: name })
    await expect(row.getByRole('cell', { name: 'local', exact: true })).toBeVisible()
    await expect(row).toContainText('no secret')

    // Storage events (SRC-3): the URL is shown once and needs no session.
    await row.getByRole('button', { name: 'Enable events' }).click()
    await row.getByRole('button', { name: 'Reveal' }).click()
    const eventUrl = (await row.getByTestId('connector-event-url').textContent()) ?? ''
    expect(eventUrl).toMatch(/\/api\/v1\/connectors\/[0-9a-f-]+\/events\?token=evt_/)
    await row.getByRole('button', { name: 'Done' }).click()
    await expect(row).toContainText('events on')

    const store = await playwright.request.newContext()
    try {
      const s3Event = {
        data: {
          Records: [
            {
              eventName: 'ObjectCreated:Put',
              s3: { bucket: { name: 'any' }, object: { key: 'a.png' } },
            },
          ],
        },
      }
      const delivered = await store.post(eventUrl, s3Event)
      expect(delivered.status(), await delivered.text()).toBe(202)
      // No project reads this connector, so nothing is queued.
      expect(await delivered.json()).toEqual({ received: 1, matched: 0, ignored: 1, job_ids: [] })

      await row.getByRole('button', { name: 'Disable events' }).click()
      await expect(row).not.toContainText('events on')
      expect((await store.post(eventUrl, s3Event)).status()).toBe(404)
    } finally {
      await store.dispose()
    }

    await row.getByRole('button', { name: 'Edit' }).click()
    const edit = page.getByRole('form', { name: `Edit ${name}` })
    await edit.getByLabel('Name').fill(`${name} renamed`)
    await edit.getByRole('button', { name: 'Save changes' }).click()
    const renamed = page.getByRole('row').filter({ hasText: `${name} renamed` })
    await expect(renamed).toBeVisible()

    await renamed.getByRole('button', { name: 'Delete' }).click()
    await renamed.getByRole('button', { name: 'Confirm delete' }).click()
    await expect(page.getByRole('row').filter({ hasText: name })).toHaveCount(0)
  } finally {
    await sweep(api, '/api/v1/connectors', (row) => String(row.name).startsWith(name))
  }
})

test('models: the compose model answers its check and lists its versions', async ({ page }) => {
  await page.goto('/models')
  await expect(page.getByRole('heading', { name: 'Models', level: 1 })).toBeVisible()

  const row = page.getByRole('row').filter({ hasText: DEMO_MODEL_NAME })
  await expect(row.getByRole('cell', { name: 'detect', exact: true })).toBeVisible()
  await row.getByRole('button', { name: 'Check' }).click()
  await expect(checkResult(page)).toHaveText(/^OK/)

  await row.getByRole('button', { name: 'Versions' }).click()
  await expect(page.getByRole('heading', { name: 'Versions', level: 3 })).toBeVisible()
  await expect(page.getByText(/^v1 · /)).toBeVisible()
  await expect(page.getByText('No versions yet.')).toHaveCount(0)
})

test('organisation webhooks: created with a one-time secret, paused, resumed and deleted', async ({
  page,
  api,
}) => {
  const url = `https://example.com/e2e-hook-${Date.now()}`
  try {
    await page.goto('/webhooks')
    await expect(page.getByRole('heading', { name: 'Organisation webhooks' })).toBeVisible()

    const form = page.getByRole('form', { name: 'New webhook' })
    await form.getByLabel('Webhook URL').fill(url)
    await form.getByRole('button', { name: 'Add webhook' }).click()

    await expect(page.getByText('Signing secret — shown once, copy it now.')).toBeVisible()
    await page.getByRole('button', { name: 'Done' }).click()
    await expect(page.getByText('Signing secret — shown once, copy it now.')).toHaveCount(0)

    const hook = page.getByRole('listitem').filter({ hasText: url })
    await expect(hook).toContainText('all events')
    await hook.getByRole('button', { name: 'Pause' }).click()
    await expect(hook).toContainText('paused')
    await hook.getByRole('button', { name: 'Resume' }).click()
    await expect(hook).not.toContainText('paused')

    await hook.getByRole('button', { name: 'Delete' }).click()
    await hook.getByRole('button', { name: 'Confirm delete' }).click()
    await expect(page.getByRole('listitem').filter({ hasText: url })).toHaveCount(0)
  } finally {
    await sweep(api, '/api/v1/webhooks', (row) => row.url === url)
  }
})

test('users: the directory lists the signed-in admin and filters by search', async ({ page }) => {
  await page.goto('/settings/users')
  await expect(page.getByRole('heading', { name: 'Users', level: 1 })).toBeVisible()

  const me = page.getByRole('listitem', { name: credentials.email })
  await expect(me).toContainText('admin')
  await expect(me).toContainText('you')

  await page.getByRole('searchbox').fill(`nobody-${Date.now()}`)
  await expect(page.getByText('No users match')).toBeVisible()
  await page.getByRole('searchbox').fill(credentials.email.split('@')[0])
  await expect(page.getByRole('listitem', { name: credentials.email })).toBeVisible()
})

test('licence: the page shows the licence in force', async ({ page }) => {
  await page.goto('/settings/license')
  await expect(page.getByRole('heading', { name: 'Licence', level: 1 })).toBeVisible()
  const details = page.getByRole('region', { name: 'Licence details' })
  await expect(details.getByRole('heading', { name: 'Licence in force' })).toBeVisible()
  await expect(details).toContainText('Edition')
})
