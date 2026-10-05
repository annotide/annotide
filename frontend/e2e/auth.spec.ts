/**
 * Local login (AUTH-2) and the route guard, starting from a signed-out browser.
 */
import { credentials, expect, signIn, test } from './support'

// These tests exercise the login form itself, so they must not inherit the
// signed-in storage state the rest of the suite runs with.
test.use({ storageState: { cookies: [], origins: [] } })

test('unauthenticated visitors are sent to the login page', async ({ page }) => {
  await page.goto('/')
  await expect(page).toHaveURL(/\/login$/)
  await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible()
})

test('a wrong password shows the API error and stays on the form', async ({ page }) => {
  await page.goto('/login')
  await page.getByLabel('Email').fill(credentials.email)
  await page.getByLabel('Password').fill('definitely-not-the-password')
  await page.getByRole('button', { name: 'Sign in' }).click()

  await expect(page.getByRole('alert')).toContainText(/incorrect email or password/i)
  await expect(page).toHaveURL(/\/login$/)
})

test('signing in lands on the project list and signing out returns to login', async ({ page }) => {
  await signIn(page)
  await expect(page).toHaveURL(/\/$/)

  // The session survives a reload: the token lives in localStorage.
  await page.reload()
  await expect(page.getByRole('heading', { name: 'Projects' })).toBeVisible()

  await page.getByTestId('account-menu').click()
  await page.getByRole('button', { name: 'Sign out' }).click()
  await expect(page).toHaveURL(/\/login$/)

  // And the guard is back in force once the token is gone.
  await page.goto('/')
  await expect(page).toHaveURL(/\/login$/)
})

test('the deep link a visitor was sent away from is restored after login', async ({ page }) => {
  await page.goto('/connectors')
  await expect(page).toHaveURL(/\/login$/)

  await page.getByLabel('Email').fill(credentials.email)
  await page.getByLabel('Password').fill(credentials.password)
  await page.getByRole('button', { name: 'Sign in' }).click()

  await expect(page).toHaveURL(/\/connectors$/)
})
