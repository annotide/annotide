/**
 * The dev Keycloak from docker-compose.sso.yml, for the opt-in SSO suites
 * (`sso.spec.ts` for OIDC, `sso-saml.spec.ts` for SAML).
 */
import type { APIRequestContext } from '@playwright/test'
import { expect } from './support'

export const KEYCLOAK = process.env.E2E_KEYCLOAK_URL ?? 'http://localhost:8180'
const REALM = 'annotation'

/** Keycloak admin REST, authenticated as the bootstrap admin of the dev container. */
export class KeycloakAdmin {
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
