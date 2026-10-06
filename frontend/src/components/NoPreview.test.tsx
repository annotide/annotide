import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { NoPreview } from './NoPreview'

describe('NoPreview', () => {
  it('says the text of a PDF in text mode is still being extracted', () => {
    render(<NoPreview meta={{ pdf_text: { status: 'pending' } }} fallback="No preview" />)
    expect(screen.getByText(/still being extracted/)).toBeInTheDocument()
  })

  it('shows why the extraction failed', () => {
    render(<NoPreview meta={{ pdf_text: { status: 'failed', error: 'encrypted' } }} fallback="No preview" />)
    expect(screen.getByText(/could not be extracted from this PDF: encrypted/)).toBeInTheDocument()
  })

  it('falls back for any other item', () => {
    render(<NoPreview meta={{}} fallback="No preview" />)
    expect(screen.getByText('No preview')).toBeInTheDocument()
  })
})
