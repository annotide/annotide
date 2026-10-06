/**
 * `item.meta.pdf_text` of a PDF taken in as text (CONTRACTS "PDF text mode"),
 * read defensively: `meta` is free-form JSON on the wire.
 */
import type { Item } from '@/api/types'

export interface PdfTextMeta {
  status: 'pending' | 'ready' | 'failed'
  error?: string
  /** Code-point range `[start, end)` of each page in the extracted text. */
  pages?: number[][]
}

export function pdfTextOf(meta: Item['meta'] | undefined): PdfTextMeta | null {
  const raw = meta?.pdf_text
  if (!raw || typeof raw !== 'object') return null
  const { status, error, pages } = raw as Record<string, unknown>
  if (status !== 'pending' && status !== 'ready' && status !== 'failed') return null
  const validPages =
    Array.isArray(pages) &&
    pages.every(
      (page) => Array.isArray(page) && page.length === 2 && page.every((n) => Number.isInteger(n)),
    )
  return {
    status,
    error: typeof error === 'string' ? error : undefined,
    pages: validPages ? (pages as number[][]) : undefined,
  }
}
