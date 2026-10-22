import taxonomy from '../../../backend/app/category_taxonomy.json'
export const categories: readonly { slug: string; label: string }[] = taxonomy
export function categoryLabel(value: string | null | undefined): string {
  return categories.find(c => c.slug === value || c.label === value)?.label ?? value ?? 'Other'
}
