// Only real application destinations, never arbitrary URLs or redirecting routes.
export function safeReturnTo(value: unknown): string | null {
  if (typeof value !== 'string') return null
  if (['/', '/rooms', '/paths', '/profile', '/saved', '/settings', '/creator/analytics', '/creator', '/admin', '/admin/users', '/admin/rooms', '/admin/paths', '/admin/labs', '/admin/audit', '/admin/infrastructure', '/paths/mine'].includes(value)) return value
  if (/^\/learners\/[A-Za-z0-9_.-]{3,50}$/.test(value)) return value
  const match = /^\/(?:(?:rooms|paths)\/([1-9][0-9]{0,9})|paths\/([1-9][0-9]{0,9})\/edit|creator\/analytics\/rooms\/([1-9][0-9]{0,9}))$/.exec(value)
  return match && Number(match[1] || match[2] || match[3]) <= 2147483647 ? value : null
}
export function authDestination(search: string, from?: unknown): string {
  return safeReturnTo(new URLSearchParams(search).get('returnTo')) || safeReturnTo(from) || '/'
}
export function authLink(path: '/login' | '/register', destination: string): string {
  const target = safeReturnTo(destination)
  return target && target !== '/' ? `${path}?returnTo=${encodeURIComponent(target)}` : path
}
export function rememberReturnTo(destination: string) {
  try { sessionStorage.setItem('secforge.auth.returnTo', safeReturnTo(destination) || '/') } catch { /* Storage may be unavailable; ordinary password redirects still use router state. */ }
}
export function consumeReturnTo(): string {
  try {
    const destination = safeReturnTo(sessionStorage.getItem('secforge.auth.returnTo')) || '/'
    sessionStorage.removeItem('secforge.auth.returnTo')
    return destination
  } catch { return '/' }
}
