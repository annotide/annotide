/**
 * SCIM provisioning (AUTH-3) against the real stack: the admin turns SCIM
 * on in the Users page, and an "IdP" — a request context carrying only the
 * SCIM token — provisions a person, deactivates them (the Users page shows
 * it), puts them in a group and deprovisions them.
 *
 * Minting replaces any SCIM token the dev stack already had. SCIM is turned
 * off again afterwards if it was off before. The deprovisioned person stays
 * as an inactive account, as SCIM DELETE never erases (SEC-6).
 */
import { expect, test } from './support'

test('SCIM: an IdP provisions, deactivates and deprovisions a person', async ({
  page,
  api,
  playwright,
}) => {
  const before = (await (await api.get('/api/v1/scim/token')).json()) as { enabled: boolean }
  const email = `scim-${Date.now()}@example.com`

  await page.goto('/settings/users')
  const panel = page.locator('section', {
    has: page.getByRole('heading', { name: /SCIM provisioning/ }),
  })
  await panel.getByRole('button', { name: before.enabled ? 'New token' : 'Turn on SCIM' }).click()
  const baseUrl = (await panel.getByTestId('scim-base-url').textContent())?.trim() ?? ''
  expect(baseUrl).toMatch(/\/api\/v1\/scim\/v2$/)
  await panel.getByRole('button', { name: 'Show token' }).click()
  const token = (await panel.getByTestId('scim-token').textContent())?.trim() ?? ''
  expect(token).toMatch(/^scim_/)
  await panel.getByRole('button', { name: 'Done' }).click()
  await expect(panel.getByRole('heading', { name: /SCIM provisioning/ })).toContainText('on')

  const idp = await playwright.request.newContext({
    extraHTTPHeaders: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/scim+json',
    },
  })
  let userId: string | null = null
  try {
    const created = await idp.post(`${baseUrl}/Users`, {
      data: {
        schemas: ['urn:ietf:params:scim:schemas:core:2.0:User'],
        userName: email,
        displayName: 'SCIM Person',
        externalId: `ext-${email}`,
        active: true,
      },
    })
    expect(created.status()).toBe(201)
    userId = ((await created.json()) as { id: string }).id

    const found = await idp.get(`${baseUrl}/Users`, {
      params: { filter: `userName eq "${email}"` },
    })
    expect(((await found.json()) as { totalResults: number }).totalResults).toBe(1)

    await page.getByRole('searchbox').fill(email)
    const row = page.getByRole('listitem', { name: email })
    await expect(row).toContainText('SCIM Person')
    await expect(row).not.toContainText('inactive')

    const deactivated = await idp.patch(`${baseUrl}/Users/${userId}`, {
      data: {
        schemas: ['urn:ietf:params:scim:api:messages:2.0:PatchOp'],
        Operations: [{ op: 'Replace', path: 'active', value: 'False' }],
      },
    })
    expect(deactivated.status()).toBe(200)
    await page.reload()
    await page.getByRole('searchbox').fill(email)
    await expect(page.getByRole('listitem', { name: email })).toContainText('inactive')

    const group = await idp.post(`${baseUrl}/Groups`, {
      data: { displayName: `E2E group ${Date.now()}`, members: [{ value: userId }] },
    })
    expect(group.status()).toBe(201)
    const groupBody = (await group.json()) as { id: string; members: Array<{ value: string }> }
    expect(groupBody.members.map((m) => m.value)).toEqual([userId])
    expect((await idp.delete(`${baseUrl}/Groups/${groupBody.id}`)).status()).toBe(204)

    expect((await idp.delete(`${baseUrl}/Users/${userId}`)).status()).toBe(204)
    expect((await idp.get(`${baseUrl}/Users/${userId}`)).status()).toBe(404)
    userId = null

    // A wrong token is a SCIM 401, not a problem-details body.
    const refused = await playwright.request.newContext()
    const anonymous = await refused.get(`${baseUrl}/Users`)
    expect(anonymous.status()).toBe(401)
    expect(((await anonymous.json()) as { schemas: string[] }).schemas).toEqual([
      'urn:ietf:params:scim:api:messages:2.0:Error',
    ])
    await refused.dispose()
  } finally {
    if (userId) await idp.delete(`${baseUrl}/Users/${userId}`)
    await idp.dispose()
    if (!before.enabled) await api.delete('/api/v1/scim/token')
  }
})
