import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { PIXELS } from './measure'
import { RulerPanel } from './RulerPanel'

describe('RulerPanel', () => {
  it('reads out the length and calibrates from a known distance', () => {
    const onCalibrate = vi.fn()
    render(<RulerPanel pixels={200} measured={200} scale={PIXELS} onCalibrate={onCalibrate} />)

    const panel = screen.getByRole('region', { name: 'Measurement' })
    expect(panel).toHaveTextContent('200 px (200 px, no scale set)')
    const set = screen.getByRole('button', { name: 'Set scale' })
    expect(set).toBeDisabled()

    fireEvent.change(screen.getByLabelText('True length'), { target: { value: '50' } })
    fireEvent.change(screen.getByLabelText('Unit'), { target: { value: 'cm' } })
    fireEvent.click(set)
    expect(onCalibrate).toHaveBeenCalledWith(0.25, 'cm')
  })

  it('only reads out when calibration is not offered', () => {
    render(
      <RulerPanel
        pixels={100}
        measured={5}
        scale={{ x: 0.05, y: 0.05, unit: 'mm', source: 'project' }}
      />,
    )
    expect(screen.getByRole('region', { name: 'Measurement' })).toHaveTextContent(
      '5 mm (100 px, project scale)',
    )
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})
