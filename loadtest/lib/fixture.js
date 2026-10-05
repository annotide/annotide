/**
 * Setup and teardown for the load test: a throwaway project full of items
 * with open annotate tasks, and one service account + API key per virtual
 * user.
 *
 * - The project borrows the seeded demo project's connectors and label
 *   schema (`make seed`). Its items are registered directly with synthetic
 *   paths: no object is read, since media never passes through the API —
 *   the browser fetches it from storage on a signed URL.
 * - Service accounts are named `loadtest-001`, `loadtest-002`, … and reused
 *   across runs (they take no seat, LIC-23); each run mints fresh keys and
 *   revokes them at the end. A key per virtual user keeps each under its own
 *   rate limit, as real annotators are.
 * - Teardown hard-deletes the project (items, tasks and annotations
 *   cascade) and revokes the keys.
 */
import http from 'k6/http'
import { sleep } from 'k6'

import { config } from './config.js'

const API = `${config.baseUrl}/api/v1`
const BATCH = 50
const BULK_MAX = 500

/** One setup/teardown request, retried while rate limited. */
function call(method, path, body, token, name) {
  const params = {
    headers: { 'Content-Type': 'application/json' },
    tags: { name: `setup ${name}` },
    timeout: '60s',
  }
  if (token) params.headers.Authorization = `Bearer ${token}`
  const payload = body === undefined ? null : JSON.stringify(body)
  for (let attempt = 0; ; attempt += 1) {
    const response = http.request(method, `${API}${path}`, payload, params)
    if (response.status !== 429 || attempt >= 10) return response
    sleep(Number(response.headers['Retry-After'] ?? 1))
  }
}

function expect(response, statuses, what) {
  if (!statuses.includes(response.status)) {
    throw new Error(`${what}: HTTP ${response.status} ${String(response.body).slice(0, 300)}`)
  }
  return response
}

export function login() {
  const response = call('POST', '/auth/login', {
    email: config.email,
    password: config.password,
  })
  expect(response, [200], `login as ${config.email}`)
  return response.json('access_token')
}

function demoProject(token) {
  const list = expect(
    call('GET', '/projects?limit=200', undefined, token, 'projects'),
    [200],
    'list projects',
  )
  const demo = list.json('items').find((project) => project.name === config.demoProject)
  if (!demo) throw new Error(`project "${config.demoProject}" not found — run \`make seed\` first`)
  const project = expect(
    call('GET', `/projects/${demo.id}`, undefined, token, 'project'),
    [200],
    'read demo',
  )
  const schemas = expect(
    call('GET', `/projects/${demo.id}/schemas`, undefined, token, 'schemas'),
    [200],
    'read demo schemas',
  ).json()
  const latest = schemas.reduce((a, b) => (b.version > a.version ? b : a))
  return { project: project.json(), schema: latest.definition }
}

function createItems(token, projectId, connectorId) {
  const ids = []
  for (let start = 0; start < config.items; start += BATCH) {
    const requests = []
    for (let index = start; index < Math.min(start + BATCH, config.items); index += 1) {
      requests.push({
        method: 'POST',
        url: `${API}/projects/${projectId}/items`,
        body: JSON.stringify({
          connector_id: connectorId,
          path: `loadtest/item-${String(index).padStart(6, '0')}.jpg`,
          media_type: 'image',
          size_bytes: 250000,
          width: 1280,
          height: 720,
        }),
        params: {
          headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${token}`,
          },
          tags: { name: 'setup item' },
          timeout: '60s',
        },
      })
    }
    for (const response of http.batch(requests)) {
      ids.push(expect(response, [200, 201], 'create item').json('id'))
    }
  }
  return ids
}

function openTasks(token, projectId, itemIds) {
  for (let start = 0; start < itemIds.length; start += BULK_MAX) {
    const body = {
      action: 'assign',
      type: 'annotate',
      item_ids: itemIds.slice(start, start + BULK_MAX),
    }
    expect(
      call('POST', `/projects/${projectId}/items/bulk`, body, token, 'bulk'),
      [200],
      'open tasks',
    )
  }
}

/**
 * Reuse or create `loadtest-NNN` service accounts, one per virtual user, and
 * give each a key and the annotator role. k6 numbers virtual users across all
 * scenarios, so a virtual user picks its account by that number: two virtual
 * users sharing an account would claim the same task (`/tasks/next` hands a
 * caller their own in-progress task first).
 */
function actors(token, projectId, count, created) {
  const existing = {}
  const list = expect(
    call('GET', '/service-accounts', undefined, token, 'accounts'),
    [200],
    'list accounts',
  )
  for (const account of list.json()) {
    if (account.is_active) existing[account.display_name] = account.id
  }
  const result = []
  for (let index = 0; index < count; index += 1) {
    const name = `loadtest-${String(index + 1).padStart(3, '0')}`
    let userId = existing[name]
    if (!userId) {
      const account = call('POST', '/service-accounts', { display_name: name }, token, 'account')
      userId = expect(account, [201], `create ${name}`).json('id')
    }
    const key = call(
      'POST',
      '/api-keys',
      {
        name: `load test ${new Date().toISOString()}`,
        scopes: ['read', 'write'],
        user_id: userId,
      },
      token,
      'key',
    )
    expect(key, [201], `key for ${name}`)
    created.keyIds.push(key.json('id'))
    const member = call(
      'POST',
      `/projects/${projectId}/members`,
      { user_id: userId, role: 'annotator' },
      token,
      'member',
    )
    expect(member, [200, 201], `add ${name} to the project`)
    result.push({ userId, token: key.json('token') })
  }
  return result
}

/** Everything the virtual users need; `teardown` receives it back. */
export function createFixture() {
  const token = login()
  const created = { projectId: null, keyIds: [] }
  try {
    const demo = demoProject(token)
    const project = call(
      'POST',
      '/projects',
      {
        name: `Load test ${new Date().toISOString()}`,
        description: 'created by loadtest/annotate.js; safe to delete',
        source_connector_id: demo.project.source_connector_id,
        result_connector_id: demo.project.result_connector_id,
        source_prefix: 'loadtest/',
      },
      token,
      'project',
    )
    created.projectId = expect(project, [201], 'create project').json('id')
    const schema = call(
      'POST',
      `/projects/${created.projectId}/schemas`,
      { ...demo.schema, version: 1 },
      token,
      'schema',
    )
    const schemaId = expect(schema, [201], 'create schema').json('id')
    const itemIds = createItems(token, created.projectId, demo.project.source_connector_id)
    openTasks(token, created.projectId, itemIds)
    const users = actors(token, created.projectId, config.annotators + config.readers, created)
    return {
      projectId: created.projectId,
      schemaId,
      users,
      keyIds: created.keyIds,
    }
  } catch (error) {
    removeFixture(created, token)
    throw error
  }
}

/**
 * Seconds from the end of the load until the latest submitted versions are in
 * the result connector (every version of a sample of submitted items has a
 * `blob_path`), polled every 5 s. The outbox publisher runs once a minute, so
 * up to a minute is normal; `null` when nothing was submitted. Gives up at
 * `limit` seconds and returns that, which fails the threshold.
 */
export function outboxCatchUp(fixture, limit) {
  const token = login()
  const started = Date.now()
  const page = call(
    'GET',
    `/projects/${fixture.projectId}/items?status=submitted&limit=20`,
    undefined,
    token,
    'outbox sample',
  )
  const itemIds = expect(page, [200], 'list submitted items')
    .json('items')
    .map((item) => item.id)
  if (itemIds.length === 0) return null
  let pending = itemIds
  while (pending.length > 0) {
    pending = pending.filter((itemId) => {
      const versions = call('GET', `/items/${itemId}/annotations`, undefined, token, 'outbox poll')
      return expect(versions, [200], 'read versions')
        .json()
        .some((version) => !version.blob_path)
    })
    const elapsed = (Date.now() - started) / 1000
    if (pending.length === 0) return elapsed
    if (elapsed >= limit) return limit
    sleep(5)
  }
  return (Date.now() - started) / 1000
}

export function removeFixture(fixture, token) {
  if (config.keep) {
    console.log(`KEEP=1: leaving project ${fixture.projectId} and ${fixture.keyIds.length} keys`)
    return
  }
  const bearer = token ?? login()
  for (const keyId of fixture.keyIds) {
    call('DELETE', `/api-keys/${keyId}`, undefined, bearer, 'revoke')
  }
  if (fixture.projectId) {
    const response = call('DELETE', `/projects/${fixture.projectId}`, undefined, bearer, 'delete')
    if (response.status !== 204) {
      console.error(`could not delete project ${fixture.projectId}: HTTP ${response.status}`)
    }
  }
}
