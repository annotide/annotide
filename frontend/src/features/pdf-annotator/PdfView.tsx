/**
 * A read-only PDF page viewer for the context panel: the original PDF beside
 * its extracted text (CONTRACTS "PDF text mode"), or any PDF companion view.
 * One page at a time, fit to the panel's width, with page navigation; pdf.js
 * loads lazily through `openPdf`, as in the annotator.
 */
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { openPdf } from './pdf'
import type { PdfDocument } from './pdf'
import { safeHref } from '../../lib/url'

/** Device pixels per PDF point: sharp in a narrow panel, also on retina. */
const RENDER_SCALE = 2

/**
 * A request to show `page`. A new object each time, so asking for the page
 * already requested turns back to it after the person paged away.
 */
export interface PageFocus {
  page: number
}

export interface PdfViewProps {
  url: string
  name: string
  /** Turn to this page, e.g. the page of the span selected in the text. */
  focus?: PageFocus
  /** Injectable for tests; the real pdf.js wrapper by default. */
  open?: (url: string) => Promise<PdfDocument>
}

export function PdfView({ url, name, focus, open = openPdf }: PdfViewProps): JSX.Element {
  const { t } = useTranslation('annotator')
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const [doc, setDoc] = useState<PdfDocument | null>(null)
  const [pageNumber, setPageNumber] = useState(1)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let cancelled = false
    let opened: PdfDocument | null = null
    setDoc(null)
    setFailed(false)
    setPageNumber(1)
    open(url).then(
      (document) => {
        opened = document
        if (cancelled) void document.destroy()
        else setDoc(document)
      },
      () => {
        if (!cancelled) setFailed(true)
      },
    )
    return () => {
      cancelled = true
      if (opened) void opened.destroy()
    }
  }, [open, url])

  useEffect(() => {
    if (doc && focus) setPageNumber(Math.min(Math.max(1, focus.page), doc.pageCount))
  }, [doc, focus])

  useEffect(() => {
    if (!doc) return
    let cancelled = false
    doc
      .page(pageNumber)
      .then((page) => {
        if (!cancelled && canvasRef.current) return page.render(canvasRef.current, RENDER_SCALE)
        return undefined
      })
      .catch(() => {
        if (!cancelled) setFailed(true)
      })
    return () => {
      cancelled = true
    }
  }, [doc, pageNumber])

  if (failed) return <p className="text-xs text-danger">{t('views.loadError')}</p>
  if (!doc) return <p className="text-xs text-muted">{t('views.loading')}</p>
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2 text-xs">
        <button
          type="button"
          className="rounded border border-line px-1.5 disabled:opacity-40"
          disabled={pageNumber <= 1}
          onClick={() => setPageNumber((n) => n - 1)}
          aria-label={t('views.previousPage')}
        >
          ‹
        </button>
        <span data-testid="pdf-view-page" className="text-muted">
          {t('views.pageOf', { page: pageNumber, count: doc.pageCount })}
        </span>
        <button
          type="button"
          className="rounded border border-line px-1.5 disabled:opacity-40"
          disabled={pageNumber >= doc.pageCount}
          onClick={() => setPageNumber((n) => n + 1)}
          aria-label={t('views.nextPage')}
        >
          ›
        </button>
        <a href={safeHref(url)} target="_blank" rel="noreferrer" className="ml-auto text-accent underline">
          {t('views.open', { name })}
        </a>
      </div>
      <div className="max-h-[60vh] overflow-auto rounded-md border border-line bg-white">
        <canvas
          ref={canvasRef}
          data-testid="pdf-view-canvas"
          aria-label={t('views.pdfPage', { name, page: pageNumber })}
          className="block w-full"
        />
      </div>
    </div>
  )
}
