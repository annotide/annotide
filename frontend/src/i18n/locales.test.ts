import { describe, expect, it } from 'vitest'
import { NAMESPACES, resources } from './resources'

type Tree = { [key: string]: string | Tree }

function flatten(tree: Tree, prefix = ''): Map<string, string> {
  const out = new Map<string, string>()
  for (const [key, value] of Object.entries(tree)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (typeof value === 'string') out.set(path, value)
    else for (const [k, v] of flatten(value, path)) out.set(k, v)
  }
  return out
}

const placeholders = (text: string): string[] =>
  [...text.matchAll(/{{\s*(\w+)\s*}}|<(\w+)>/g)].map((m) => m[1] ?? m[2]).sort()

/** Every translation, checked against English (the source language). */
const translations = Object.entries(resources as Record<string, Record<string, Tree>>).filter(
  ([code]) => code !== 'en',
)

describe.each(NAMESPACES)('locale namespace %s', (ns) => {
  const en = flatten(resources.en[ns] as Tree)

  it('has no empty strings', () => {
    for (const [code, namespaces] of [['en', resources.en] as const, ...translations])
      for (const [key, text] of flatten(namespaces[ns] as Tree))
        expect({ code, key, text: text.trim() }).not.toEqual({ code, key, text: '' })
  })

  it('has the same keys, placeholders and tags in every translation', () => {
    for (const [code, namespaces] of translations) {
      const other = flatten(namespaces[ns] as Tree)
      expect({ code, keys: [...other.keys()].sort() }).toEqual({ code, keys: [...en.keys()].sort() })
      for (const [key, text] of en)
        expect({ code, key, tokens: placeholders(other.get(key) ?? '') }).toEqual({
          code,
          key,
          tokens: placeholders(text),
        })
    }
  })
})
