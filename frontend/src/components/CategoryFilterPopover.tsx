import { useState } from 'react'
import { categories } from '../lib/categories'
import AnchoredPopover from './AnchoredPopover'
import Icon from './Icon'

function CategoryChoices({ values, onChange, close }: { values: string[]; onChange: (values: string[]) => void; close: () => void }) {
  const [search, setSearch] = useState('')
  const matches = categories.filter(category => category.label.toLowerCase().includes(search.trim().toLowerCase()))
  return <>
    <label className="category-search">Search categories<input type="search" value={search} onChange={event => setSearch(event.target.value)}
      onKeyDown={event => { if (event.key === 'ArrowDown') { event.preventDefault(); event.currentTarget.closest('[role="dialog"]')?.querySelector<HTMLInputElement>('input[type="checkbox"]:not(:disabled)')?.focus() } }} /></label>
    <p className="muted">Matches any selected category. Choose up to eight.</p>
    <div className="category-options" role="group" aria-label="Available categories" onKeyDown={event => {
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
      const options = Array.from(event.currentTarget.querySelectorAll<HTMLInputElement>('input:not(:disabled)'))
      if (!options.length) return
      event.preventDefault()
      const index = options.indexOf(event.target as HTMLInputElement)
      options[event.key === 'Home' ? 0 : event.key === 'End' ? options.length - 1 : (index + (event.key === 'ArrowUp' ? -1 : 1) + options.length) % options.length].focus()
    }}>
      {matches.map(category => <label key={category.slug}><input type="checkbox" checked={values.includes(category.slug)} disabled={!values.includes(category.slug) && values.length >= 8}
        onChange={event => onChange(event.target.checked ? [...values, category.slug] : values.filter(value => value !== category.slug))} /><span>{category.label}</span></label>)}
      {!matches.length && <p className="muted" role="status">No matching categories</p>}
    </div>
    <div className="category-popover-footer"><button type="button" className="btn btn-ghost btn-sm" disabled={!values.length} onClick={() => onChange([])}>Clear selection</button><span className="muted" role="status">{values.length} selected</span><button type="button" className="btn btn-secondary btn-sm" onClick={close}>Done</button></div>
  </>
}

export default function CategoryFilterPopover({ values, onChange }: { values: string[]; onChange: (values: string[]) => void }) {
  return <AnchoredPopover className="category-filter" buttonClassName="category-filter-trigger" accessibleLabel={`Filter by category${values.length ? `, ${values.length} selected` : ''}`}
    label={<><span>Categories{values.length ? ` (${values.length})` : ''}</span><Icon name="chevron" size={16} /></>}>
    {close => <CategoryChoices values={values} onChange={onChange} close={close} />}
  </AnchoredPopover>
}
