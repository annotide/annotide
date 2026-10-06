/**
 * Playwright end-to-end configuration (NFR-8).
 *
 * The suite drives the real product: a running backend (Postgres, Redis,
 * Azurite, worker) seeded with `make seed`, and the frontend in front of it.
 * Nothing is mocked. Two ways to point it at a stack:
 *
 * - Default: Playwright starts the Vite dev server from the working tree on
 *   :5174 (`E2E_PORT`) and proxies `/api` to `VITE_API_PROXY_TARGET`
 *   (default http://localhost:8000, the compose backend). Its own port, not
 *   :5173, so a stale compose `frontend` image is never tested by mistake.
 * - `E2E_BASE_URL=https://host` skips the dev server and targets that
 *   deployment as built.
 *
 * Credentials come from `E2E_EMAIL` / `E2E_PASSWORD` and default to the
 * `make seed` superuser. See e2e/README.md.
 */
import { defineConfig, devices } from '@playwright/test'

const port = Number(process.env.E2E_PORT ?? 5174)
const baseURL = process.env.E2E_BASE_URL ?? `http://localhost:${port}`
const apiProxyTarget = process.env.VITE_API_PROXY_TARGET ?? 'http://localhost:8000'
const isCI = Boolean(process.env.CI)

export default defineConfig({
  testDir: './e2e',
  outputDir: './test-results',
  fullyParallel: true,
  forbidOnly: isCI,
  retries: isCI ? 2 : 0,
  workers: isCI ? 2 : undefined,
  reporter: isCI ? [['github'], ['html', { open: 'never' }]] : [['list']],
  timeout: 30_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL,
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: [
    // Logs in once through the UI and saves the browser storage; every other
    // project starts from that state instead of repeating the login form.
    { name: 'setup', testMatch: /.*\.setup\.ts/ },
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'], storageState: 'e2e/.auth/admin.json' },
      dependencies: ['setup'],
    },
  ],
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: `npm run dev -- --port ${port} --strictPort`,
        url: baseURL,
        reuseExistingServer: !isCI,
        timeout: 60_000,
        env: { VITE_API_PROXY_TARGET: apiProxyTarget },
      },
})
