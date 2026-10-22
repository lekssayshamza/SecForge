import { useEffect } from 'react'
export function confirmDiscard(dirty: boolean): boolean {
  return !dirty || window.confirm('Discard your unsaved changes? Answers and instructional text have not been saved.')
}
type Guard = { url: string; state: { idx?: number } | null }
const guards = new Set<Guard>()
export function confirmUnsavedNavigation(): boolean {
  return confirmDiscard(guards.size > 0)
}
let initialized = false, restoring = false
// Install before BrowserRouter: its pop listener may synchronously unmount an
// editor and remove that editor's listeners before a later listener can run.
export function initializeUnsavedNavigationGuard() {
  if (initialized) return
  initialized = true
  window.addEventListener('popstate', event => {
    if (restoring) { restoring = false; event.stopImmediatePropagation(); return }
    const guard = [...guards].find(g => { const original = new URL(g.url); return location.pathname !== original.pathname || location.search !== original.search })
    if (!guard || confirmDiscard(true)) return
    event.stopImmediatePropagation()
    const index = guard.state?.idx
    const delta = typeof index === 'number' && typeof event.state?.idx === 'number' ? index - event.state.idx : 0
    if (delta) { restoring = true; window.history.go(delta) }
    else window.history.replaceState(guard.state, '', guard.url)
  }, true)
}
export function useUnsavedChanges(dirty: boolean) {
  useEffect(() => {
    if (!dirty) return
    initializeUnsavedNavigationGuard()
    const guard = { url: window.location.href, state: window.history.state }
    guards.add(guard)
    const unload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    const click = (event: MouseEvent) => {
      const link = (event.target as Element)?.closest?.('a[href]') as HTMLAnchorElement | null
      if (!link || event.ctrlKey || event.metaKey || event.shiftKey || link.target === '_blank' || link.download || link.origin === location.origin && link.pathname === location.pathname && link.search === location.search) return
      if (!confirmDiscard(true)) { event.preventDefault(); event.stopImmediatePropagation() }
    }
    window.addEventListener('beforeunload', unload)
    document.addEventListener('click', click, true)
    return () => { guards.delete(guard); window.removeEventListener('beforeunload', unload); document.removeEventListener('click', click, true) }
  }, [dirty])
}
