import { useTheme } from '../context/ThemeContext'
import Icon from './Icon'
export default function ThemeToggle() {
  const { theme, toggle } = useTheme()
  return <button type="button" className="btn btn-ghost icon-button theme-toggle" onClick={toggle}
    aria-label={`Switch to ${theme === 'light' ? 'dark' : 'light'} theme`} aria-pressed={theme === 'dark'}>
    <Icon name={theme === 'light' ? 'moon' : 'sun'} size={19} />
  </button>
}
