import { describe, expect, it } from 'vitest'

import { safeHref } from './url'

describe('safeHref', () => {
  it('keeps http, https and relative URLs', () => {
    expect(safeHref('https://mlflow.example/#/runs/1')).toBe('https://mlflow.example/#/runs/1')
    expect(safeHref('http://localhost:5001/runs/2')).toBe('http://localhost:5001/runs/2')
    expect(safeHref('/api/v1/media/abc?sig=1')).toBe('/api/v1/media/abc?sig=1')
  })

  it('drops script, data and non-string values', () => {
    expect(safeHref('javascript:alert(1)')).toBeUndefined()
    expect(safeHref(' JavaScript:alert(1)')).toBeUndefined()
    expect(safeHref('data:text/html,<script>1</script>')).toBeUndefined()
    expect(safeHref('')).toBeUndefined()
    expect(safeHref(42)).toBeUndefined()
    expect(safeHref(undefined)).toBeUndefined()
  })
})
