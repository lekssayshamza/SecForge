const HAS_TIMEZONE = /(Z|[+-]\d{2}:?\d{2})$/

function parseDate(value: string): Date {
  return new Date(HAS_TIMEZONE.test(value) ? value : `${value}Z`)
}

export function formatDate(value: string): string {
  const date = parseDate(value)
  if (Number.isNaN(date.getTime())) return 'Not available'
  return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

export function formatDateTime(value: string): string {
  const date = parseDate(value)
  if (Number.isNaN(date.getTime())) return 'Not available'
  return date.toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}
