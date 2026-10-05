/**
 * Workload profiles and latency targets for the k6 load test (NFR-1…4).
 *
 * Every value can be overridden with `-e NAME=value` (or the environment
 * variable of the same name), e.g. `-e ANNOTATORS=120 -e HOLD=15m`.
 *
 * The targets are placeholders until NFR-1…4 are confirmed: the overall
 * p95 of 1 s matches the API latency alert (`alerts.thresholds.
 * apiLatencyP95Seconds` in the Helm chart); the per-request ones are what
 * an annotator notices — claiming a task, opening an item, saving.
 */

const env = __ENV

function num(name, fallback) {
  const raw = env[name]
  if (raw === undefined || raw === '') return fallback
  const value = Number(raw)
  if (!Number.isFinite(value)) throw new Error(`${name} must be a number, got "${raw}"`)
  return value
}

function str(name, fallback) {
  const raw = env[name]
  return raw === undefined || raw === '' ? fallback : raw
}

const PROFILES = {
  // Seconds, a couple of people: checks the scripts and the stack, not capacity.
  smoke: {
    annotators: 2,
    readers: 1,
    items: 20,
    rampUp: '5s',
    hold: '30s',
    think: 1,
  },
  // A mid-sized team working through a project at human pace.
  default: {
    annotators: 50,
    readers: 5,
    items: 1000,
    rampUp: '1m',
    hold: '5m',
    think: 10,
  },
  // Four times that, for finding where it bends.
  stress: {
    annotators: 200,
    readers: 20,
    items: 5000,
    rampUp: '3m',
    hold: '10m',
    think: 10,
  },
}

const profileName = str('PROFILE', 'default')
const profile = PROFILES[profileName]
if (!profile) {
  throw new Error(
    `PROFILE must be one of ${Object.keys(PROFILES).join(', ')}, got "${profileName}"`,
  )
}

export const config = {
  profile: profileName,
  baseUrl: str('BASE_URL', 'http://localhost:8001').replace(/\/$/, ''),
  email: str('EMAIL', 'admin@example.com'),
  password: str('PASSWORD', 'admin-dev-password'),
  // The seeded project whose connectors and label schema the test borrows.
  demoProject: str('DEMO_PROJECT', 'Demo: traffic objects'),
  annotators: num('ANNOTATORS', profile.annotators),
  readers: num('READERS', profile.readers),
  items: num('ITEMS', profile.items),
  rampUp: str('RAMP_UP', profile.rampUp),
  hold: str('HOLD', profile.hold),
  // Mean seconds between an annotator's actions (±50 % jitter).
  think: num('THINK', profile.think),
  // Draft saves per task before it is submitted or released.
  saves: num('SAVES', 3),
  // Share of claimed tasks that end in a submit; the rest are released.
  submitRatio: num('SUBMIT_RATIO', 0.3),
  // Leave the project, keys and accounts in place for inspection.
  keep: str('KEEP', '') === '1',
}

/** p95 targets in milliseconds, per request name. */
export const targets = {
  overall: num('P95_MS', 1000),
  claim: num('P95_CLAIM_MS', 500),
  item: num('P95_ITEM_MS', 300),
  history: num('P95_HISTORY_MS', 300),
  save: num('P95_SAVE_MS', 500),
  submit: num('P95_SUBMIT_MS', 750),
  release: num('P95_RELEASE_MS', 300),
  extend: num('P95_EXTEND_MS', 300),
  list: num('P95_LIST_MS', 1000),
  stats: num('P95_STATS_MS', 1000),
  tasks: num('P95_TASKS_MS', 1000),
  errorRate: num('MAX_ERROR_RATE', 0.01),
  // Seconds after the load until submitted versions are in storage (DATA-2).
  outboxCatchUp: num('MAX_OUTBOX_CATCH_UP_S', 120),
}
