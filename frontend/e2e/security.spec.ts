/**
 * The account security page (AUTH-2). Enrolment is started but never
 * confirmed: turning MFA on for the seeded admin would lock every later run
 * out. A pending seed changes nothing about sign-in and is replaced by the
 * next setup. The personal data download (SEC-6) only reads.
 */
import { expect, test } from './support'

test('setting up an authenticator shows a scannable QR code and the key', async ({ page }) => {
  await page.goto('/settings/security')
  await expect(page.getByRole('heading', { name: 'Two-step sign-in' })).toBeVisible()
  await page.getByRole('button', { name: 'Set up an authenticator app' }).click()

  const qr = page.getByRole('img', { name: 'QR code for the authenticator app' })
  await expect(qr).toBeVisible()
  expect(await qr.evaluate((node) => (node as HTMLImageElement).naturalWidth)).toBeGreaterThan(100)
  await expect(page.getByText(/^[A-Z2-7]{32}$/)).toBeVisible()
  await expect(page.getByRole('link', { name: /^otpauth:\/\/totp\// })).toBeVisible()
})

test('a signed-in user downloads their own personal data as JSON (SEC-6)', async ({ page }) => {
  await page.goto('/settings/security')
  await expect(page.getByRole('heading', { name: 'Your data' })).toBeVisible()

  const downloading = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Download my data' }).click()
  const download = await downloading

  expect(download.suggestedFilename()).toMatch(/^personal-data-[0-9a-f-]{36}\.json$/)
  const path = await download.path()
  const { readFile } = await import('node:fs/promises')
  const data = JSON.parse(await readFile(path, 'utf-8')) as {
    user: { email: string; password_hash?: unknown }
    memberships: unknown[]
    audit_events: unknown[]
  }
  expect(data.user.email).toBe('admin@example.com')
  expect(data.user.password_hash).toBeUndefined()
  expect(data.memberships.length).toBeGreaterThan(0)
  expect(data.audit_events.length).toBeGreaterThan(0)
})
