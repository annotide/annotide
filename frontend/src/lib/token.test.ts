import { describe, expect, it } from 'vitest'
import { isMfaSetupToken } from './token'

function token(claims: Record<string, unknown>): string {
  const body = btoa(JSON.stringify(claims)).replace(/\+/g, '-').replace(/\//g, '_')
  return `header.${body}.signature`
}

describe('isMfaSetupToken', () => {
  it('reads the mfa_setup claim', () => {
    expect(isMfaSetupToken(token({ sub: 'u1', mfa_setup: true }))).toBe(true)
    expect(isMfaSetupToken(token({ sub: 'u1' }))).toBe(false)
  })

  it('is false for missing or malformed tokens', () => {
    expect(isMfaSetupToken(null)).toBe(false)
    expect(isMfaSetupToken('not-a-jwt')).toBe(false)
    expect(isMfaSetupToken('a.%%%.c')).toBe(false)
  })
})
