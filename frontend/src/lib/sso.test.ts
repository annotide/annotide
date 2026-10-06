import { beforeEach, describe, expect, it } from 'vitest'

import { claimSilentAttempt, isSsoToken } from './sso'

/** An unsigned JWT-shaped string; `isSsoToken` only reads the payload. */
function fakeToken(claims: Record<string, unknown>): string {
  const payload = btoa(JSON.stringify(claims))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')
  return `header.${payload}.signature`
}

describe('isSsoToken', () => {
  it('reads the sso claim', () => {
    expect(isSsoToken(fakeToken({ sub: 'u', sso: true }))).toBe(true)
    expect(isSsoToken(fakeToken({ sub: 'u' }))).toBe(false)
    expect(isSsoToken(fakeToken({ sub: 'u', sso: 'yes' }))).toBe(false)
  })

  it('is false for anything that is not a token', () => {
    expect(isSsoToken(null)).toBe(false)
    expect(isSsoToken('')).toBe(false)
    expect(isSsoToken('no-dots')).toBe(false)
    expect(isSsoToken('a.%%%.c')).toBe(false)
  })
})

describe('claimSilentAttempt', () => {
  beforeEach(() => sessionStorage.clear())

  it('allows one attempt per minute', () => {
    expect(claimSilentAttempt(1_000_000)).toBe(true)
    expect(claimSilentAttempt(1_030_000)).toBe(false)
    expect(claimSilentAttempt(1_061_000)).toBe(true)
  })
})
