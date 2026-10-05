/**
 * Personal API keys (AUTH-4): mint one from the settings page, see the token
 * once, prove it authenticates a request, revoke it and prove it no longer does.
 */
import { expect, test } from './support'

test('a key minted in the UI works as a bearer token until it is revoked', async ({
  page,
  playwright,
  baseURL,
}) => {
  const name = `e2e ${Date.now()}`

  await page.goto('/settings/api-keys')
  await expect(page.getByRole('heading', { name: 'API keys' })).toBeVisible()

  // The admin is a superuser, so the service accounts panel is on the page too.
  const form = page.getByRole('form', { name: 'Create API key' })
  await form.getByLabel('Name', { exact: true }).fill(name)
  await form.getByLabel('Expires on').fill('')
  await form.getByRole('button', { name: 'Create key' }).click()

  const token = (await page.getByLabel('API key token').textContent())?.trim() ?? ''
  expect(token.length).toBeGreaterThan(20)

  // The key authenticates on its own, without the browser's session.
  const asKey = await playwright.request.newContext({
    baseURL,
    extraHTTPHeaders: { Authorization: `Bearer ${token}` },
  })
  const before = await asKey.get('/api/v1/projects', { params: { limit: 1 } })
  expect(before.status()).toBe(200)

  // Dismissing the banner leaves only metadata behind.
  await page.getByRole('button', { name: 'Dismiss' }).click()
  await expect(page.getByLabel('API key token')).toHaveCount(0)
  const row = page.getByRole('row').filter({ hasText: name })
  await expect(row.getByText('active')).toBeVisible()

  // Revoking takes a confirming second click.
  const revoke = row.getByRole('button', { name: `Revoke ${name}` })
  await revoke.click()
  await expect(page.getByText(/Click Revoke again to confirm/)).toBeVisible()
  await revoke.click()
  await expect(row.getByText('revoked')).toBeVisible()
  await expect(row.getByRole('button', { name: `Revoke ${name}` })).toHaveCount(0)

  const after = await asKey.get('/api/v1/projects', { params: { limit: 1 } })
  expect(after.status()).toBe(401)
  await asKey.dispose()
})

test('a service account key works until the account is deactivated', async ({
  page,
  playwright,
  baseURL,
}) => {
  const account = `e2e svc ${Date.now()}`

  await page.goto('/settings/api-keys')
  await page.getByLabel('Display name').fill(account)
  await page.getByRole('button', { name: 'Create service account' }).click()

  const card = page.getByRole('listitem', { name: account })
  await expect(card.getByText(/@service\.invalid$/)).toBeVisible()
  await card.getByRole('button', { name: `Show keys for ${account}` }).click()

  const form = card.getByRole('form', { name: `Create key for ${account}` })
  await form.getByLabel('Name', { exact: true }).fill('ci')
  await form.getByLabel('Expires on').fill('')
  await form.getByRole('button', { name: 'Create key' }).click()

  const token = (await card.getByLabel('API key token').textContent())?.trim() ?? ''
  expect(token.length).toBeGreaterThan(20)

  const asKey = await playwright.request.newContext({
    baseURL,
    extraHTTPHeaders: { Authorization: `Bearer ${token}` },
  })
  const before = await asKey.get('/api/v1/projects', { params: { limit: 1 } })
  expect(before.status()).toBe(200)

  // Deactivation takes a confirming second click and revokes the account's keys.
  const deactivate = card.getByRole('button', { name: `Deactivate ${account}` })
  await deactivate.click()
  await deactivate.click()
  await expect(card.getByText('deactivated')).toBeVisible()
  await expect(card.getByRole('row').filter({ hasText: 'ci' }).getByText('revoked')).toBeVisible()

  const after = await asKey.get('/api/v1/projects', { params: { limit: 1 } })
  expect(after.status()).toBe(401)
  await asKey.dispose()
})

test('a superuser adds a service account to a project from the Members picker', async ({
  page,
  api,
  playwright,
  baseURL,
}) => {
  const stamp = Date.now()
  const project = await api.post('/api/v1/projects', {
    data: { name: `e2e members ${stamp}`, description: 'created by the Playwright suite' },
  })
  expect(project.status(), await project.text()).toBe(201)
  const projectId = ((await project.json()) as { id: string }).id
  const account = await api.post('/api/v1/service-accounts', {
    data: { display_name: `e2e picker ${stamp}` },
  })
  expect(account.status(), await account.text()).toBe(201)
  const accountId = ((await account.json()) as { id: string }).id

  try {
    const key = await api.post('/api/v1/api-keys', {
      data: { name: 'picker', scopes: ['read'], user_id: accountId },
    })
    expect(key.status(), await key.text()).toBe(201)
    const token = ((await key.json()) as { token: string }).token
    const asKey = await playwright.request.newContext({
      baseURL,
      extraHTTPHeaders: { Authorization: `Bearer ${token}` },
    })
    const listed = async (): Promise<boolean> => {
      const res = await asKey.get('/api/v1/projects', { params: { limit: 100 } })
      const { items } = (await res.json()) as { items: Array<{ id: string }> }
      return items.some((item) => item.id === projectId)
    }
    // Membership is what opens a project to the key, in the list and by id.
    expect(await listed()).toBe(false)
    expect((await asKey.get(`/api/v1/projects/${projectId}`)).ok()).toBe(false)

    await page.goto(`/projects/${projectId}/settings`)
    await page
      .getByLabel('new-member-service-account')
      .selectOption({ label: `e2e picker ${stamp}` })
    await page.getByLabel('new-member-role').selectOption('viewer')
    await page.getByRole('button', { name: 'Add member' }).click()
    await expect(page.getByRole('row').filter({ hasText: /@service\.invalid/ })).toBeVisible()

    expect((await asKey.get(`/api/v1/projects/${projectId}`)).status()).toBe(200)
    expect(await listed()).toBe(true)
    await asKey.dispose()
  } finally {
    await api.delete(`/api/v1/service-accounts/${accountId}`)
    await api.delete(`/api/v1/projects/${projectId}`)
  }
})
