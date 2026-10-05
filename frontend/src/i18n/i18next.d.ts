import 'i18next'
import type { resources } from './resources'

// Typed keys: `t('shell:nav.users')` fails `tsc` when the key is missing.
declare module 'i18next' {
  interface CustomTypeOptions {
    defaultNS: 'common'
    resources: (typeof resources)['en']
  }
}
