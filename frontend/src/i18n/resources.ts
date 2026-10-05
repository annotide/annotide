/**
 * Translation resources (UX-8), one JSON file per namespace and language.
 * English is the source language; any other language must have exactly the
 * same keys (enforced by `locales.test.ts`).
 */
import enAdmin from './locales/en/admin.json'
import enAnnotator from './locales/en/annotator.json'
import enAuth from './locales/en/auth.json'
import enCommon from './locales/en/common.json'
import enProjects from './locales/en/projects.json'
import enSettings from './locales/en/settings.json'
import enShell from './locales/en/shell.json'
import enUsers from './locales/en/users.json'

export const resources = {
  en: {
    common: enCommon,
    shell: enShell,
    auth: enAuth,
    users: enUsers,
    projects: enProjects,
    settings: enSettings,
    admin: enAdmin,
    annotator: enAnnotator,
  },
} as const

export type Namespace = keyof (typeof resources)['en']
export const NAMESPACES = Object.keys(resources.en) as Namespace[]
