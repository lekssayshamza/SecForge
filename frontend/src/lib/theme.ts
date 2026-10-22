export type Theme = 'light' | 'dark'
export const THEME_KEY = 'secforge-theme'
export function resolveTheme(value: string | null): Theme {
  return value === 'dark' ? 'dark' : 'light'
}
export function readTheme(): Theme {
  try { return resolveTheme(localStorage.getItem(THEME_KEY)) }
  catch { return 'light' }
}
export function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme
  document.documentElement.style.colorScheme = theme
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', theme === 'dark' ? '#121821' : '#F7F8FA')
  document.querySelector('link[rel="icon"]')?.setAttribute('href', `/favicon-${theme}.svg`)
  try { localStorage.setItem(THEME_KEY, theme) } catch { /* Theme still works when storage is unavailable. */ }
}
