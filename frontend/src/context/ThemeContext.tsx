import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { applyTheme, readTheme, resolveTheme, THEME_KEY, type Theme } from '../lib/theme'

const ThemeContext = createContext<{ theme: Theme; toggle: () => void } | null>(null)
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(readTheme)
  useEffect(() => { applyTheme(theme) }, [theme])
  useEffect(() => {
    const sync = (event: StorageEvent) => {
      if (event.key === THEME_KEY || event.key === null) setTheme(resolveTheme(event.newValue))
    }
    window.addEventListener('storage', sync)
    return () => window.removeEventListener('storage', sync)
  }, [])
  return <ThemeContext.Provider value={{ theme, toggle: () => setTheme(current => current === 'light' ? 'dark' : 'light') }}>{children}</ThemeContext.Provider>
}
export function useTheme() {
  const context = useContext(ThemeContext)
  if (!context) throw new Error('ThemeProvider is required')
  return context
}
