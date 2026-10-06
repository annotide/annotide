/**
 * pdf.js behind a lazy import, so the ~1 MB library and its worker load only
 * when a PDF item is opened. Everything the annotator needs from a page is
 * reduced to plain data here: its size in points, a render call and its words.
 */
import type { Word } from './words'
import { wordBoxes } from './words'
import type { TextRun } from './words'

export interface PdfPage {
  /** Page size in PDF points at scale 1, rotation applied. */
  width: number
  height: number
  /** Draws the page into `canvas` at `scale` device pixels per point. */
  render: (canvas: HTMLCanvasElement, scale: number) => Promise<void>
  words: () => Promise<Word[]>
}

export interface PdfDocument {
  pageCount: number
  page: (pageNumber: number) => Promise<PdfPage>
  destroy: () => Promise<void>
}

export async function openPdf(url: string): Promise<PdfDocument> {
  const [pdfjs, worker] = await Promise.all([
    import('pdfjs-dist'),
    import('pdfjs-dist/build/pdf.worker.min.mjs?url'),
  ])
  pdfjs.GlobalWorkerOptions.workerSrc = worker.default
  // pdf.js 6 compiles nothing at runtime, so the CSP's script-src 'self' holds.
  const task = pdfjs.getDocument({ url })
  const doc = await task.promise

  return {
    pageCount: doc.numPages,
    async page(pageNumber) {
      const page = await doc.getPage(pageNumber)
      const viewport = page.getViewport({ scale: 1 })
      return {
        width: viewport.width,
        height: viewport.height,
        async render(canvas, scale) {
          const scaled = page.getViewport({ scale })
          canvas.width = Math.floor(scaled.width)
          canvas.height = Math.floor(scaled.height)
          await page.render({ canvas, viewport: scaled }).promise
        },
        async words() {
          const content = await page.getTextContent()
          const runs = content.items.filter((item): item is TextRun & typeof item => 'str' in item)
          return wordBoxes(runs, viewport.transform)
        },
      }
    },
    destroy: () => task.destroy(),
  }
}
