/**
 * What the centre pane says when an item has no `media_url`: usually the
 * connector cannot sign, but a PDF taken in as text (CONTRACTS "PDF text
 * mode") has none until the worker has extracted its text, or when that
 * failed.
 */
import { useTranslation } from 'react-i18next'

import type { Item } from '@/api/types'
import { pdfTextOf } from '@/lib/pdfText'

export function NoPreview({ meta, fallback }: { meta: Item['meta']; fallback: string }): JSX.Element {
  const { t } = useTranslation('annotator')
  const pdfText = pdfTextOf(meta)
  let message = fallback
  if (pdfText?.status === 'pending') message = t('shared.pdfTextPending')
  else if (pdfText?.status === 'failed') message = t('shared.pdfTextFailed', { error: pdfText.error ?? '' })
  return <p className="text-sm text-muted">{message}</p>
}
