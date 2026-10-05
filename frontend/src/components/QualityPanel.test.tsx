import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api } from '@/api/client'
import type { ProjectAgreement } from '@/api/types'

import { QualityPanel } from './QualityPanel'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      getAgreement: vi.fn(),
      getAnnotatorQuality: vi.fn(),
      openGoldTasks: vi.fn(),
    },
  }
})

const AGREEMENT: ProjectAgreement = {
  items: 12,
  annotators: [
    { user_id: 'u1', email: 'anna@example.com', display_name: 'Anna', items: 12 },
    { user_id: 'u2', email: 'ben@example.com', display_name: 'Ben', items: 12 },
  ],
  classification: [{ field: 'weather', items: 12, fleiss_kappa: 0.71, krippendorff_alpha: 0.72 }],
  shapes: { mean_iou: 0.83, f1: 0.9, iou_threshold: 0.5, envelope_iou: true },
  spans: { f1_exact: null, f1_overlap: null },
  pairs: [
    {
      a: 'u1',
      b: 'u2',
      items: 12,
      cohen_kappa: 0.7,
      mean_iou: 0.83,
      shape_f1: 0.9,
      span_f1_exact: null,
      span_f1_overlap: null,
    },
  ],
}

function renderPanel() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <QualityPanel projectId="p1" />
    </QueryClientProvider>,
  )
}

describe('QualityPanel', () => {
  afterEach(() => vi.clearAllMocks())

  it('shows agreement, pairs by name and annotator accuracy', async () => {
    vi.mocked(api.getAgreement).mockResolvedValue(AGREEMENT)
    vi.mocked(api.getAnnotatorQuality).mockResolvedValue({
      annotators: [
        {
          user_id: 'u1',
          email: 'anna@example.com',
          display_name: 'Anna',
          gold_items: 5,
          classification_accuracy: 0.8,
          shape_f1: 0.9,
          mean_iou: 0.85,
          span_f1: null,
          score: 0.85,
        },
      ],
    })
    renderPanel()

    expect(await screen.findByText(/12 items · shape F1 0.90/)).toBeInTheDocument()
    expect(screen.getByText('Anna · Ben')).toBeInTheDocument()
    expect(screen.getByText('0.71')).toBeInTheDocument()
    expect(screen.getByText(/compared by their bounding boxes/)).toBeInTheDocument()
    expect(await screen.findByText('0.85', { selector: 'td.font-semibold' })).toBeInTheDocument()
  })

  it('explains empty states and opens gold tasks', async () => {
    vi.mocked(api.getAgreement).mockResolvedValue({ ...AGREEMENT, items: 0, pairs: [] })
    vi.mocked(api.getAnnotatorQuality).mockResolvedValue({ annotators: [] })
    vi.mocked(api.openGoldTasks).mockResolvedValue({ opened: 3, skipped: 1 })
    renderPanel()

    expect(await screen.findByText(/No item has two or more consensus versions/)).toBeInTheDocument()
    expect(await screen.findByText(/No gold results yet/)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Open gold tasks' }))
    await waitFor(() => expect(api.openGoldTasks).toHaveBeenCalledWith('p1', undefined))
    expect(await screen.findByText('Opened 3 gold tasks, 1 already existed.')).toBeInTheDocument()
  })
})
