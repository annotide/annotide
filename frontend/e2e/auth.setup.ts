/**
 * One-off login that the `chromium` project depends on. The session lives in
 * localStorage (`annotation.auth`), which storageState captures alongside
 * cookies, so every later test opens the app already signed in.
 */
import { test as setup } from '@playwright/test'

import { STORAGE_STATE, signIn } from './support'

setup('sign in as the seeded superuser', async ({ page }) => {
  await signIn(page)
  await page.context().storageState({ path: STORAGE_STATE })
})
