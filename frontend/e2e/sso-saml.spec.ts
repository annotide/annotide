/**
 * SAML 2.0 single sign-on and IdP group sync (AUTH-1, AUTH-3) against a real
 * Keycloak: its SAML client `annotide-saml` signs the assertion, and the
 * backend checks it with xmlsec1, as in production.
 *
 * Opt-in like `sso.spec.ts`: needs the docker-compose.sso.yml stack, then
 * `E2E_SSO=1 npx playwright test sso-saml`. Skipped otherwise.
 */
import type { APIRequestContext, Browser, Page } from '@playwright/test'
import { KEYCLOAK, KeycloakAdmin } from './keycloak'
import { DEMO_PROJECT_NAME, expect, test } from './support'

/** The realm's SAML user; alice and carol sign in through OIDC, and one account links one IdP. */
const ERIN = { username: 'erin', password: 'erin-dev-password', email: 'erin.saml@example.com' }

test.skip(!process.env.E2E_SSO, 'set E2E_SSO=1 with the docker-compose.sso.yml stack running')

/** Sign in through the SAML button and Keycloak's login form, in a fresh context. */
async function signInWithSaml(browser: Browser, baseURL: string | undefined): Promise<Page> {
  const context = await browser.newContext({ baseURL, storageState: { cookies: [], origins: [] } })
  const page = await context.newPage()
  await page.goto('/login')
  await page.getByRole('link', { name: 'Sign in with Keycloak SAML' }).click()

  await page.waitForURL(`${KEYCLOAK}/**`)
  await page.locator('#username').fill(ERIN.username)
  await page.locator('#password').fill(ERIN.password)
  await page.locator('#kc-login').click()

  // Keycloak posts the SAMLResponse to the ACS, which hands over to /auth/callback.
  await expect(page.getByTestId('account-menu')).toBeVisible()
  expect(new URL(page.url()).pathname).toBe('/')
  return page
}

async function membership(
  api: APIRequestContext,
  projectId: string,
): Promise<{ role: string; source: string } | undefined> {
  const response = await api.get(`/api/v1/projects/${projectId}/members`)
  expect(response.ok()).toBeTruthy()
  const members = (await response.json()) as Array<{ email: string; role: string; source: string }>
  return members.find((member) => member.email === ERIN.email)
}

test.describe('SAML sign-in with Keycloak (AUTH-1)', () => {
  // One Keycloak realm: tests that move users between groups must not overlap.
  test.describe.configure({ mode: 'serial' })

  let projectId: string
  let keycloak: KeycloakAdmin

  test.beforeAll(async ({ playwright }) => {
    keycloak = await KeycloakAdmin.connect(await playwright.request.newContext())
  })

  test.beforeEach(async ({ api }) => {
    await keycloak.setGroups(ERIN.username, ['annotators'])
    const list = await api.get('/api/v1/projects', { params: { limit: 100 } })
    const { items } = (await list.json()) as { items: Array<{ id: string; name: string }> }
    const demoId = items.find((project) => project.name === DEMO_PROJECT_NAME)?.id
    if (!demoId) throw new Error(`"${DEMO_PROJECT_NAME}" not found — run \`make seed\`.`)
    const demo = (await (await api.get(`/api/v1/projects/${demoId}`)).json()) as {
      source_connector_id: string
      result_connector_id: string
    }
    const created = await api.post('/api/v1/projects', {
      data: {
        name: `SAML group sync ${Date.now()}`,
        description: 'created by the Playwright suite; safe to delete',
        source_connector_id: demo.source_connector_id,
        result_connector_id: demo.result_connector_id,
        settings: { idp_groups: { annotators: 'annotator', reviewers: 'reviewer' } },
      },
    })
    expect(created.status(), await created.text()).toBe(201)
    projectId = ((await created.json()) as { id: string }).id
  })

  test.afterEach(async ({ api }) => {
    await api.delete(`/api/v1/projects/${projectId}`)
    await keycloak.setGroups(ERIN.username, ['annotators'])
  })

  test('a signed assertion signs in and its groups set the project role', async ({
    api,
    browser,
    baseURL,
  }) => {
    const first = await signInWithSaml(browser, baseURL)
    expect(await membership(api, projectId)).toEqual(
      expect.objectContaining({ role: 'annotator', source: 'idp' }),
    )
    await expect(first.getByRole('button', { name: 'Admin', exact: true })).toHaveCount(0)
    await first.context().close()

    await keycloak.setGroups(ERIN.username, ['reviewers', 'platform-admins'])
    const second = await signInWithSaml(browser, baseURL)
    expect(await membership(api, projectId)).toEqual(
      expect.objectContaining({ role: 'reviewer', source: 'idp' }),
    )
    // APP_SAML_ADMIN_GROUPS=platform-admins in the override.
    await second.getByRole('button', { name: 'Admin', exact: true }).click()
    await expect(second.getByRole('link', { name: 'Users' })).toBeVisible()
    await second.context().close()
  })
})
