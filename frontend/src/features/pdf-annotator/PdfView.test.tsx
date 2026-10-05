import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import type { PdfDocument } from './pdf'
import { PdfView } from './PdfView'

function fakeDocument(pageCount: number): PdfDocument & { rendered: number[]; destroyed: boolean } {
  const doc = {
    pageCount,
    rendered: [] as number[],
    destroyed: false,
    page: (n: number) =>
      Promise.resolve({
        width: 600,
        height: 800,
        render: () => {
          doc.rendered.push(n)
          return Promise.resolve()
        },
        words: () => Promise.resolve([]),
      }),
    destroy: () => {
      doc.destroyed = true
      return Promise.resolve()
    },
  }
  return doc
}

describe('PdfView', () => {
  it('renders the first page and pages through the document', async () => {
    const doc = fakeDocument(2)
    const { unmount } = render(<PdfView url="https://s/a.pdf" name="a.pdf" open={() => Promise.resolve(doc)} />)

    expect(await screen.findByTestId('pdf-view-page')).toHaveTextContent('Page 1 of 2')
    await waitFor(() => expect(doc.rendered).toEqual([1]))
    expect(screen.getByRole('button', { name: 'Previous page' })).toBeDisabled()

    await userEvent.click(screen.getByRole('button', { name: 'Next page' }))
    expect(screen.getByTestId('pdf-view-page')).toHaveTextContent('Page 2 of 2')
    await waitFor(() => expect(doc.rendered).toEqual([1, 2]))
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled()
    expect(screen.getByRole('link', { name: 'Open a.pdf' })).toHaveAttribute('href', 'https://s/a.pdf')

    unmount()
    expect(doc.destroyed).toBe(true)
  })

  it('turns to a requested page, again after the person paged away', async () => {
    const doc = fakeDocument(3)
    const open = (): Promise<PdfDocument> => Promise.resolve(doc)
    const { rerender } = render(<PdfView url="u" name="a.pdf" open={open} focus={{ page: 3 }} />)
    expect(await screen.findByTestId('pdf-view-page')).toHaveTextContent('Page 3 of 3')

    await userEvent.click(screen.getByRole('button', { name: 'Previous page' }))
    expect(screen.getByTestId('pdf-view-page')).toHaveTextContent('Page 2 of 3')
    rerender(<PdfView url="u" name="a.pdf" open={open} focus={{ page: 3 }} />)
    expect(screen.getByTestId('pdf-view-page')).toHaveTextContent('Page 3 of 3')

    // Out of range is clamped to the document.
    rerender(<PdfView url="u" name="a.pdf" open={open} focus={{ page: 9 }} />)
    expect(screen.getByTestId('pdf-view-page')).toHaveTextContent('Page 3 of 3')
  })

  it('says so when the PDF cannot be opened', async () => {
    const open = vi.fn(() => Promise.reject(new Error('CORS')))
    render(<PdfView url="https://s/a.pdf" name="a.pdf" open={open} />)
    expect(await screen.findByText('This view could not be loaded.')).toBeInTheDocument()
  })
})
