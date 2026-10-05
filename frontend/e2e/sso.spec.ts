/**
 * Single sign-on and IdP group sync (AUTH-1, AUTH-3) against a real Keycloak.
 *
 * Opt-in: needs the dev Keycloak from docker-compose.sso.yml and the backend
 * started with that override, then `E2E_SSO=1 npx playwright test sso`.
 * See frontend/e2e/README.md. Skipped otherwise, so `make e2e` is unchanged.
 */
import type { APIRequestContext, Browser, Page } from '@playwright/test'
import { DEMO_PROJECT_NAME, expect, test } from './support'

const KEYCLOAK = process.env.E2E_KEYCLOAK_URL ?? 'http://localhost:8180'
const REALM = 'annotation'

const ALICE = { username: 'alice', password: 'alice-dev-password', email: 'alice.sso@example.com' }
const CAROL = { username: 'carol', password: 'carol-dev-password', email: 'carol.sso@example.com' }

test.skip(!process.env.E2E_SSO, 'set E2E_SSO=1 with the docker-compose.sso.yml stack running')

/** Keycloak admin REST, authenticated as the bootstrap admin of the dev container. */
class KeycloakAdmin {
  private constructor(
    private readonly http: APIRequestContext,
    private readonly token: string,
  ) {}

  static async connect(http: APIRequestContext): Promise<KeycloakAdmin> {
    const response = await http.post(`${KEYCLOAK}/realms/master/protocol/openid-connect/token`, {
      form: { grant_type: 'password', client_id: 'admin-cli', username: 'admin', password: 'admin' },
    })
    expect(response.ok(), `Keycloak admin login: ${response.status()}`).toBeTruthy()
    const { access_token } = (await response.json()) as { access_token: string }
    return new KeycloakAdmin(http, access_token)
  }

  private url(path: string): string {
    return `${KEYCLOAK}/admin/realms/${REALM}${path}`
  }

  private get headers(): Record<string, string> {
    return { Authorization: `Bearer ${this.token}` }
  }

  private async userId(username: string): Promise<string> {
    const response = await this.http.get(this.url('/users'), {
      headers: this.headers,
      params: { username, exact: 'true' },
    })
    const [user] = (await response.json()) as Array<{ id: string }>
    if (!user) throw new Error(`Keycloak user ${username} not found — realm not imported?`)
    return user.id
  }

  /** Make `groups` exactly the user's group memberships. */
  async setGroups(username: string, groups: string[]): Promise<void> {
    const id = await this.userId(username)
    const all = (await (
      await this.http.get(this.url('/groups'), { headers: this.headers })
    ).json()) as Array<{ id: string; name: string }>
    for (const group of all) {
      const path = this.url(`/users/${id}/groups/${group.id}`)
      const response = groups.includes(group.name)
        ? await this.http.put(path, { headers: this.headers })
        : await this.http.delete(path, { headers: this.headers })
      expect(response.status(), `${group.name} for ${username}`).toBe(204)
    }
  }
}

/** Sign in through the app's SSO button and Keycloak's own login form, in a fresh context. */
async function signInWithKeycloak(
  browser: Browser,
  baseURL: string | undefined,
  user: { username: string; password: string },
): Promise<Page> {
  const context = await browser.newContext({ baseURL, storageState: { cookies: [], origins: [] } })
  const page = await context.newPage()
  await page.goto('/login')
  await page.getByRole('link', { name: 'Sign in with Keycloak' }).click()

  await page.waitForURL(`${KEYCLOAK}/**`)
  await page.locator('#username').fill(user.username)
  await page.locator('#password').fill(user.password)
  await page.locator('#kc-login').click()

  // Back through /auth/callback to the project list.
  await expect(page.getByTestId('account-menu')).toBeVisible()
  expect(new URL(page.url()).pathname).toBe('/')
  return page
}

async function membership(
  api: APIRequestContext,
  projectId: string,
  email: string,
): Promise<{ role: string; source: string } | undefined> {
  const response = await api.get(`/api/v1/projects/${projectId}/members`)
  expect(response.ok()).toBeTruthy()
  const members = (await response.json()) as Array<{ email: string; role: string; source: string }>
  return members.find((member) => member.email === email)
}

test.describe('SSO group sync with Keycloak (AUTH-3)', () => {
  let projectId: string
  let keycloak: KeycloakAdmin

  test.beforeAll(async ({ playwright }) => {
    keycloak = await KeycloakAdmin.connect(await playwright.request.newContext())
    await keycloak.setGroups(ALICE.username, ['annotators'])
    await keycloak.setGroups(CAROL.username, ['platform-admins'])
  })

  test.beforeEach(async ({ api }) => {
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
        name: `SSO group sync ${Date.now()}`,
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
    await keycloak.setGroups(ALICE.username, ['annotators'])
  })

  test('groups in the ID token set and then change the project role', async ({
    api,
    browser,
    baseURL,
  }) => {
    const first = await signInWithKeycloak(browser, baseURL, ALICE)
    expect(await membership(api, projectId, ALICE.email)).toEqual(
      expect.objectContaining({ role: 'annotator', source: 'idp' }),
    )
    // Not an admin: the superuser-only nav stays hidden.
    await expect(first.getByRole('button', { name: 'Admin' })).toHaveCount(0)
    await first.context().close()

    // Moved to another group at the provider: the next sign-in follows it.
    await keycloak.setGroups(ALICE.username, ['reviewers'])
    const second = await signInWithKeycloak(browser, baseURL, ALICE)
    expect(await membership(api, projectId, ALICE.email)).toEqual(
      expect.objectContaining({ role: 'reviewer', source: 'idp' }),
    )
    await second.context().close()

    // In no mapped group at all: the IdP-granted membership goes.
    await keycloak.setGroups(ALICE.username, [])
    const third = await signInWithKeycloak(browser, baseURL, ALICE)
    expect(await membership(api, projectId, ALICE.email)).toBeUndefined()
    await third.context().close()
  })

  test('the admin group makes a superuser', async ({ browser, baseURL }) => {
    const page = await signInWithKeycloak(browser, baseURL, CAROL)
    await page.getByRole('button', { name: 'Admin' }).click()
    await expect(page.getByRole('link', { name: 'Users' })).toBeVisible()
    await page.context().close()
  })
})
