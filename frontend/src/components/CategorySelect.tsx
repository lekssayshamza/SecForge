import { categories } from '../lib/categories'
export default function CategorySelect({ value, onChange, id, label = 'Category', disabled = false, filter = false }: { value: string; onChange: (value: string) => void; id?: string; label?: string; disabled?: boolean; filter?: boolean }) {
  return <select id={id} aria-label={label} value={value} onChange={e => onChange(e.target.value)} disabled={disabled} required={!filter}>
    <option value="">{filter ? 'All categories' : 'Select a category'}</option>{value && !categories.some(c => c.slug === value) && <option value={value} disabled>{value} — choose a supported category</option>}
    {categories.map(c => <option key={c.slug} value={c.slug}>{c.label}</option>)}
  </select>
}
