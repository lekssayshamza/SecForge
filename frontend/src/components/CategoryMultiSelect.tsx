import { categories, categoryLabel } from '../lib/categories'
export default function CategoryMultiSelect({ values, onChange, id, disabled = false, filter = false }: { values: string[]; onChange: (values: string[]) => void; id: string; disabled?: boolean; filter?: boolean }) {
  return <div className="category-multi">
    <select id={id} aria-label={filter ? 'Filter by category' : 'Add room category'} value="" disabled={disabled || values.length >= 8} onChange={e => { if (e.target.value && !values.includes(e.target.value)) onChange([...values, e.target.value]) }}>
      <option value="">{values.length ? 'Add another category…' : filter ? 'All categories — select to filter' : 'Choose room categories…'}</option>{categories.filter(c => !values.includes(c.slug)).map(c => <option key={c.slug} value={c.slug}>{c.label}</option>)}
    </select>
    <div className="category-chips" aria-label={filter ? 'Selected category filters' : 'Selected room categories'}>{values.map((value, index) => <button key={value} type="button" className="category-chip" disabled={disabled} aria-label={`Remove ${categoryLabel(value)}`} onClick={() => onChange(values.filter(c => c !== value))}>{categoryLabel(value)}{!filter && index === 0 && <span className="sr-only"> (primary category)</span>} <span aria-hidden="true">×</span></button>)}</div>
    <p className="form-hint">{filter ? 'Matches any selected category.' : 'Choose up to eight categories.'}</p>
  </div>
}
