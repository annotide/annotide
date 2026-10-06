import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { AttributeEditor, type AttributeField } from './AttributeEditor'

const fields: AttributeField[] = [
  { name: 'occluded', type: 'boolean', required: false },
  { name: 'make', type: 'select', required: true, options: ['Toyota', 'Volvo'] },
  { name: 'tags', type: 'multiselect', required: false, options: ['red', 'dented'] },
  { name: 'count', type: 'number', required: false },
  { name: 'note', type: 'text', required: false },
]

describe('AttributeEditor', () => {
  it('renders a placeholder when the class has no attributes', () => {
    render(<AttributeEditor fields={[]} values={{}} onChange={vi.fn()} />)
    expect(screen.getByText('No attributes defined.')).toBeInTheDocument()
  })

  it('marks required fields and flags a missing required value', () => {
    render(<AttributeEditor fields={fields} values={{}} onChange={vi.fn()} />)
    const make = screen.getByLabelText('make *')
    expect(make).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByLabelText('note')).not.toHaveAttribute('aria-invalid')
  })

  it('emits typed values: boolean, select, number, text', async () => {
    const onChange = vi.fn()
    render(<AttributeEditor fields={fields} values={{}} onChange={onChange} />)
    const user = userEvent.setup()

    await user.click(screen.getByLabelText('occluded'))
    expect(onChange).toHaveBeenLastCalledWith('occluded', true)

    await user.selectOptions(screen.getByLabelText('make *'), 'Volvo')
    expect(onChange).toHaveBeenLastCalledWith('make', 'Volvo')

    await user.type(screen.getByLabelText('count'), '4')
    expect(onChange).toHaveBeenLastCalledWith('count', 4)

    await user.type(screen.getByLabelText('note'), 'x')
    expect(onChange).toHaveBeenLastCalledWith('note', 'x')
  })

  it('clears a field (undefined) instead of storing an empty string', async () => {
    const onChange = vi.fn()
    render(
      <AttributeEditor fields={fields} values={{ make: 'Volvo', note: 'x' }} onChange={onChange} />,
    )
    const user = userEvent.setup()

    await user.selectOptions(screen.getByLabelText('make *'), '')
    expect(onChange).toHaveBeenLastCalledWith('make', undefined)

    await user.clear(screen.getByLabelText('note'))
    expect(onChange).toHaveBeenLastCalledWith('note', undefined)
  })

  it('multiselect toggles options in and out of a string array', async () => {
    const onChange = vi.fn()
    const { rerender } = render(
      <AttributeEditor fields={fields} values={{}} onChange={onChange} />,
    )
    const user = userEvent.setup()

    await user.click(screen.getByLabelText('red'))
    expect(onChange).toHaveBeenLastCalledWith('tags', ['red'])

    rerender(<AttributeEditor fields={fields} values={{ tags: ['red', 'dented'] }} onChange={onChange} />)
    expect(screen.getByLabelText('dented')).toBeChecked()

    await user.click(screen.getByLabelText('red'))
    expect(onChange).toHaveBeenLastCalledWith('tags', ['dented'])

    rerender(<AttributeEditor fields={fields} values={{ tags: ['dented'] }} onChange={onChange} />)
    await user.click(screen.getByLabelText('dented'))
    expect(onChange).toHaveBeenLastCalledWith('tags', undefined)
  })

  it('disables every control when read-only', () => {
    render(<AttributeEditor fields={fields} values={{ make: 'Volvo' }} onChange={vi.fn()} readOnly />)
    expect(screen.getByLabelText('make *')).toBeDisabled()
    expect(screen.getByLabelText('occluded')).toBeDisabled()
    expect(screen.getByLabelText('red')).toBeDisabled()
    expect(screen.getByLabelText('note')).toBeDisabled()
  })
})
