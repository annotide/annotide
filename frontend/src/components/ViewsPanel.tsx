/**
 * Companion views (§5 multimodal): the other files of the same task, shown
 * beside the annotator, e.g. the caption of an image or the source text of
 * a document. Text is shown as text, images as images, PDFs page by page
 * (e.g. the original beside its extracted text, CONTRACTS "PDF text mode"),
 * anything else as a link. Judgements across views go in the classification
 * panel.
 */

import { lazy, Suspense } from 'react'
import { useTranslation } from 'react-i18next'

import { useItemText, useItemViews } from '@/api/queries'
import type { ItemView } from '@/api/types'
import type { PageFocus } from '@/features/pdf-annotator'
import { safeHref } from '../lib/url'

// pdf.js is a ~1 MB chunk: load it only when a PDF view is shown.
const PdfView = lazy(() =>
  import('@/features/pdf-annotator').then((mod) => ({ default: mod.PdfView })),
)

function TextView({ url }: { url: string }): JSX.Element {
  const { t } = useTranslation('annotator')
  const text = useItemText(url)
  if (text.isError) return <p className="text-xs text-danger">{t('views.loadError')}</p>
  if (text.data === undefined) return <p className="text-xs text-muted">{t('views.loading')}</p>
  return (
    <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md border border-line bg-surface p-2 text-xs text-ink">
      {text.data}
    </pre>
  )
}

function View({ view, pdfFocus }: { view: ItemView; pdfFocus?: PageFocus }): JSX.Element {
  const { t } = useTranslation('annotator')
  const name = view.label || view.path.split('/').pop() || view.path
  let body: JSX.Element
  if (!view.url) {
    body = <p className="text-xs text-muted">{t('views.unavailable')}</p>
  } else if (view.media_type === 'text') {
    body = <TextView url={view.url} />
  } else if (view.media_type === 'pdf') {
    body = (
      <Suspense fallback={<p className="text-xs text-muted">{t('views.loading')}</p>}>
        <PdfView url={view.url} name={name} focus={pdfFocus} />
      </Suspense>
    )
  } else if (view.media_type === 'image') {
    body = (
      <img
        src={view.url}
        alt={name}
        crossOrigin="anonymous"
        className="max-h-64 w-full rounded-md border border-line object-contain"
      />
    )
  } else {
    body = (
      <a href={safeHref(view.url)} target="_blank" rel="noreferrer" className="text-xs text-accent underline">
        {t('views.open', { name })}
      </a>
    )
  }
  return (
    <figure className="space-y-1">
      <figcaption className="text-xs font-medium text-muted" title={view.path}>
        {name}
      </figcaption>
      {body}
    </figure>
  )
}

export function ViewsPanel({
  itemId,
  meta,
  pdfFocus,
}: {
  itemId: string
  meta: Record<string, unknown> | null | undefined
  /** The page PDF views should turn to (PDF text mode: the text's current page). */
  pdfFocus?: PageFocus
}): JSX.Element | null {
  const { t } = useTranslation('annotator')
  const listed = Array.isArray(meta?.views) && meta.views.length > 0
  const views = useItemViews(itemId, listed)
  if (!listed) return null
  return (
    <section aria-labelledby="views-heading" className="mb-6">
      <h2 id="views-heading" className="mb-2 text-sm font-semibold text-ink">
        {t('views.heading')}
      </h2>
      {views.isError && <p className="text-xs text-danger">{t('views.loadError')}</p>}
      <div className="space-y-3">
        {(views.data ?? []).map((view) => (
          <View key={view.path} view={view} pdfFocus={pdfFocus} />
        ))}
      </div>
    </section>
  )
}
