import { useEffect, useRef, useState } from 'react'
import { useLocation, useSearchParams } from 'react-router-dom'
import { categories } from '../lib/categories'
import type { DiscoveryParams } from '../api/discovery'
export function readDiscovery(search: string): DiscoveryParams {
  const p = new URLSearchParams(search), rawPage = Number(p.get('page'))
  const choice = (key: string, values: string[], fallback = '') => values.includes(p.get(key) || '') ? p.get(key)! : fallback
  return { q: (p.get('q') || '').slice(0, 100), category: (p.get('category') || '').split(',').filter(c => categories.some(v => v.slug === c)).slice(0, 8).filter((v, i, a) => a.indexOf(v) === i).join(','), difficulty: choice('difficulty', ['easy', 'medium', 'hard']), room_type: choice('room_type', ['THEORY', 'PRACTICAL']), progress: choice('progress', ['all', 'not-started', 'in-progress', 'completed'], 'all'), sort: choice('sort', ['newest', 'alphabetical', 'difficulty'], 'newest'), page: Number.isInteger(rawPage) && rawPage >= 1 && rawPage <= 1000 ? rawPage : 1 }
}
export function discoveryReturn(value: unknown, fallback: string): string {
  if (typeof value !== 'string' || value.length > 1800 || /[\\\u0000-\u001f]/.test(value)) return fallback
  try { const url = new URL(value, 'https://secforge.invalid'); return url.origin === 'https://secforge.invalid' && ['/rooms', '/paths', '/saved'].includes(url.pathname) && !url.hash ? url.pathname + url.search : fallback } catch { return fallback }
}
export function useDiscoveryState() {
  const location = useLocation(), [, setParams] = useSearchParams(), params = readDiscovery(location.search)
  const [input, setInput] = useState(params.q || ''), ownWrite = useRef<string | null>(null), current = useRef(new URLSearchParams(location.search))
  current.current = new URLSearchParams(location.search)
  const write = (patch: Record<string, string>, replace: boolean) => {
    const next = new URLSearchParams(current.current)
    for (const [key, value] of Object.entries(patch)) { if (!value || value === 'all' || value === 'newest' || key === 'page' && value === '1') next.delete(key); else next.set(key, value) }
    const encoded = next.toString(); ownWrite.current = encoded ? '?' + encoded : ''; current.current = next; setParams(next, { replace })
  }
  const writeRef = useRef(write); writeRef.current = write
  useEffect(() => {
    if (ownWrite.current !== location.search) setInput(readDiscovery(location.search).q || '')
    ownWrite.current = null
  }, [location.search])
  useEffect(() => {
    const normalized = input.trim().replace(/\s+/g, ' ')
    if (normalized === readDiscovery(location.search).q) return
    const timer = setTimeout(() => writeRef.current({ q: normalized, page: '1' }, true), 250)
    return () => clearTimeout(timer)
  }, [input, location.search])
  return { params, input, setInput, change: (key: string, value: string) => write({ [key]: value, page: '1', q: input.trim().replace(/\s+/g, ' ') }, false), page: (value: number) => write({ page: String(value) }, false), clear: () => { setInput(''); write({ q: '', category: '', difficulty: '', room_type: '', progress: '', sort: '', page: '' }, false) }, returnTo: location.pathname + location.search }
}
