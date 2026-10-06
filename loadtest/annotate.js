/**
 * Load test for the annotation hot path (NFR-1…4), for k6.
 *
 * Two kinds of virtual user work one throwaway project at once:
 *
 * - `annotate` — annotators: claim the next task, open the item and its
 *   history, save a few drafts with think time between them (extending the
 *   lock as the UI's heartbeat does), then submit or release.
 * - `browse`   — people on the project pages: page through the item grid
 *   (every row signs a media URL), read the dashboard stats and task list.
 *
 * Every request is tagged with a `name`; the thresholds hold the p95 of each
 * against `lib/config.js`. The worker is loaded too: every save publishes
 * to the result connector through the outbox.
 *
 *   make loadtest                      # default profile, compose stack
 *   make loadtest PROFILE=smoke        # a minute, two annotators
 *   k6 run -e BASE_URL=https://annotate.example.com loadtest/annotate.js
 *
 * See loadtest/README.md.
 */
import http from 'k6/http'
import { check, sleep } from 'k6'
import exec from 'k6/execution'
import { Counter, Trend } from 'k6/metrics'

import { config, targets } from './lib/config.js'
import { createFixture, outboxCatchUp, removeFixture } from './lib/fixture.js'

const API = `${config.baseUrl}/api/v1`

const claimed = new Counter('tasks_claimed')
const queueEmpty = new Counter('queue_empty')
const submitted = new Counter('annotations_submitted')
// Seconds after the load until submitted versions reach the result connector.
const outboxLag = new Trend('outbox_catch_up_seconds')

function stages(target) {
  return [
    { duration: config.rampUp, target },
    { duration: config.hold, target },
    { duration: '30s', target: 0 },
  ]
}

function p95(ms) {
  return [`p(95)<${ms}`]
}

const scenarios = {
  annotate: {
    executor: 'ramping-vus',
    exec: 'annotate',
    startVUs: 0,
    stages: stages(config.annotators),
    gracefulRampDown: '60s',
  },
}
if (config.readers > 0) {
  scenarios.browse = {
    executor: 'ramping-vus',
    exec: 'browse',
    startVUs: 0,
    stages: stages(config.readers),
    gracefulRampDown: '30s',
  }
}

export const options = {
  scenarios,
  setupTimeout: '10m',
  teardownTimeout: '5m',
  // Setup's own requests are tagged `setup …` and kept out of the targets.
  thresholds: {
    'http_req_duration{scenario:annotate}': p95(targets.overall),
    'http_req_failed{scenario:annotate}': [`rate<${targets.errorRate}`],
    'http_req_duration{name:claim}': p95(targets.claim),
    'http_req_duration{name:item}': p95(targets.item),
    'http_req_duration{name:history}': p95(targets.history),
    'http_req_duration{name:save}': p95(targets.save),
    'http_req_duration{name:submit}': p95(targets.submit),
    'http_req_duration{name:release}': p95(targets.release),
    'http_req_duration{name:extend}': p95(targets.extend),
    ...(config.readers > 0
      ? {
          'http_req_duration{scenario:browse}': p95(targets.overall),
          'http_req_failed{scenario:browse}': [`rate<${targets.errorRate}`],
          'http_req_duration{name:list}': p95(targets.list),
          'http_req_duration{name:stats}': p95(targets.stats),
          'http_req_duration{name:tasks}': p95(targets.tasks),
        }
      : {}),
    checks: [`rate>${1 - targets.errorRate}`],
    outbox_catch_up_seconds: [`max<${targets.outboxCatchUp}`],
  },
  summaryTrendStats: ['avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
}

export function setup() {
  const fixture = createFixture()
  console.log(
    `profile ${config.profile}: ${config.annotators} annotators, ${config.readers} readers, ` +
      `${config.items} items in project ${fixture.projectId}`,
  )
  return fixture
}

export function teardown(fixture) {
  // Latency alone misses a publisher that falls behind (it capped at 50
  // events a minute once); this catches it.
  const seconds = outboxCatchUp(fixture, targets.outboxCatchUp)
  if (seconds !== null) {
    outboxLag.add(seconds)
    console.log(`outbox caught up ${seconds.toFixed(0)} s after the load`)
  }
  removeFixture(fixture)
}

/** Each virtual user is one service account, as one person would be. */
function actor(fixture) {
  return fixture.users[(exec.vu.idInTest - 1) % fixture.users.length]
}

function params(user, name) {
  return {
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${user.token}`,
    },
    tags: { name },
  }
}

/** Think time around the configured mean, ±50 %. */
function pause(scale = 1) {
  sleep(config.think * scale * (0.5 + Math.random()))
}

function box() {
  const x = Math.random() * 1100
  const y = Math.random() * 560
  const classes = ['car', 'sign', 'pedestrian']
  return {
    id: crypto.randomUUID(),
    type: 'bbox',
    class: classes[Math.floor(Math.random() * classes.length)],
    bbox: [x, y, x + 20 + Math.random() * 150, y + 20 + Math.random() * 150],
  }
}

function annotation(fixture, task, shapes, submit) {
  return JSON.stringify({
    label_schema_version_id: fixture.schemaId,
    task_id: task.id,
    duration_ms: Math.round(config.think * 1000),
    submit,
    result: {
      schema_version: 1,
      media_type: 'image',
      classification: {},
      shapes,
    },
  })
}

export function annotate(fixture) {
  const user = actor(fixture)
  const claim = http.post(
    `${API}/tasks/next?project_id=${fixture.projectId}&type=annotate`,
    null,
    params(user, 'claim'),
  )
  if (claim.status === 204) {
    queueEmpty.add(1)
    pause()
    return
  }
  if (!check(claim, { 'claim: 200': (r) => r.status === 200 })) {
    pause()
    return
  }
  claimed.add(1)
  const task = claim.json()

  const item = http.get(`${API}/items/${task.item_id}`, params(user, 'item'))
  check(item, {
    'item: 200 with a media url': (r) => r.status === 200 && !!r.json('media_url'),
  })
  const history = http.get(`${API}/items/${task.item_id}/annotations`, params(user, 'history'))
  check(history, { 'history: 200': (r) => r.status === 200 })

  const shapes = []
  for (let save = 0; save < config.saves; save += 1) {
    pause()
    shapes.push(box())
    const draft = http.post(
      `${API}/items/${task.item_id}/annotations`,
      annotation(fixture, task, shapes, false),
      params(user, 'save'),
    )
    check(draft, { 'save: 201': (r) => r.status === 201 })
    if (save % 2 === 1) {
      const extend = http.post(`${API}/tasks/${task.id}/extend`, null, params(user, 'extend'))
      check(extend, { 'extend: 200': (r) => r.status === 200 })
    }
  }

  if (Math.random() < config.submitRatio) {
    const done = http.post(
      `${API}/items/${task.item_id}/annotations`,
      annotation(fixture, task, shapes, true),
      params(user, 'submit'),
    )
    if (check(done, { 'submit: 201': (r) => r.status === 201 })) submitted.add(1)
  } else {
    const release = http.post(`${API}/tasks/${task.id}/release`, null, params(user, 'release'))
    check(release, { 'release: 200': (r) => r.status === 200 })
  }
}

export function browse(fixture) {
  const user = actor(fixture)
  let cursor = null
  for (let page = 0; page < 3; page += 1) {
    const query = cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''
    const list = http.get(
      `${API}/projects/${fixture.projectId}/items?limit=50${query}`,
      params(user, 'list'),
    )
    if (!check(list, { 'list: 200': (r) => r.status === 200 })) break
    cursor = list.json('next_cursor')
    if (!cursor) break
    pause(0.2)
  }
  const stats = http.get(`${API}/projects/${fixture.projectId}/stats`, params(user, 'stats'))
  check(stats, { 'stats: 200': (r) => r.status === 200 })
  const tasks = http.get(
    `${API}/projects/${fixture.projectId}/tasks?limit=50`,
    params(user, 'tasks'),
  )
  check(tasks, { 'tasks: 200': (r) => r.status === 200 })
  pause()
}
