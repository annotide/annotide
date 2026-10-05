import { afterEach, describe, expect, it, vi } from 'vitest'
import { newShapeId } from './ids'

const UUID_V4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/

describe('newShapeId', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('is a v4 UUID even without crypto.randomUUID (plain-http installs)', () => {
    vi.stubGlobal('crypto', {
      getRandomValues: crypto.getRandomValues.bind(crypto),
    })
    const ids = new Set(Array.from({ length: 50 }, () => newShapeId()))
    expect(ids.size).toBe(50)
    for (const id of ids) expect(id).toMatch(UUID_V4)
  })
})
