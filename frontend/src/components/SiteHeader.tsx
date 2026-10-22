import { hasCreatorAccess } from '../lib/permissions'
import { NavLink, useLocation } from 'react-router-dom'
import { useEffect, useRef, useState } from 'react'
import { useAuth } from '../context/AuthContext'
import Logo from './Logo'
import Icon from './Icon'
import ThemeToggle from './ThemeToggle'
import Avatar from './Avatar'
import { confirmUnsavedNavigation } from '../hooks/useUnsavedChanges'

export default function SiteHeader() {
  const { user, logout } = useAuth()
  const location = useLocation()
  const [mobileOpen, setMobileOpen] = useState(false)
  const [accountOpen, setAccountOpen] = useState(false)
  const account = useRef<HTMLDivElement>(null)
  const accountButton = useRef<HTMLButtonElement>(null)
  const menuButton = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (!accountOpen) return
    const frame = requestAnimationFrame(() => account.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus())
    return () => cancelAnimationFrame(frame)
  }, [accountOpen])
  const menuKeys = (event: React.KeyboardEvent) => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return
    const items = Array.from(account.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') || [])
    if (!items.length) return
    event.preventDefault()
    const index = items.indexOf(document.activeElement as HTMLElement)
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : (index + (event.key === 'ArrowUp' ? -1 : 1) + items.length) % items.length
    items[next].focus()
  }
  useEffect(() => {
    setMobileOpen(false); setAccountOpen(false)
  }, [location.pathname])
  useEffect(() => {
    if (!mobileOpen) return
    const frame = requestAnimationFrame(() => document.querySelector<HTMLAnchorElement>('#main-navigation a')?.focus())
    return () => cancelAnimationFrame(frame)
  }, [mobileOpen])
  useEffect(() => {
    const dismiss = (event: PointerEvent) => {
      if (!account.current?.contains(event.target as Node)) setAccountOpen(false)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      if (accountOpen) { setAccountOpen(false); accountButton.current?.focus() }
      else if (mobileOpen) { setMobileOpen(false); menuButton.current?.focus() }
    }
    document.addEventListener('pointerdown', dismiss)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', dismiss); document.removeEventListener('keydown', escape) }
  }, [accountOpen, mobileOpen])
  const creator = hasCreatorAccess(user)
  return <>
    <a href="#main-content" className="skip-link">Skip to content</a>
    <header className="site-header"><div className="navbar">
      <NavLink to="/" className="brand" aria-label="SecForge home"><Logo size={30} /><span className="wordmark">SecForge</span></NavLink>
      <nav id="main-navigation" className={`main-nav${mobileOpen ? ' is-open' : ''}`} aria-label="Main">
        <NavLink to="/" end className={({ isActive }) => `nav-item${isActive ? ' is-active' : ''}`}><Icon name="grid" size={17} /> {user ? 'Overview' : 'Home'}</NavLink>
        <NavLink to="/rooms" className={({ isActive }) => `nav-item${isActive ? ' is-active' : ''}`}><Icon name="compass" size={17} /> Explore rooms</NavLink>
        <NavLink to="/paths" className={({ isActive }) => `nav-item${isActive ? ' is-active' : ''}`}><Icon name="compass" size={17} /> Learning paths</NavLink>
        <NavLink to="/leaderboard" className={({ isActive }) => `nav-item${isActive ? ' is-active' : ''}`}><Icon name="check" size={17} /> Leaderboard</NavLink>
        {!user && <NavLink className="nav-item guest-mobile-auth" to="/login">Log In</NavLink>}
      </nav>
      <div className="nav-actions"><ThemeToggle />
        {user ? <div className="account" ref={account} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setAccountOpen(false) }}>
          <button type="button" ref={accountButton} className="account-button" aria-label="Account options" aria-haspopup="menu" aria-expanded={accountOpen} aria-controls="account-options" onKeyDown={event => { if (event.key === 'ArrowDown') { event.preventDefault(); setAccountOpen(true) } }} onClick={() => { setAccountOpen(!accountOpen); setMobileOpen(false) }}>
            <Avatar user={user} decorative /><span className="account-name">{user.display_name || user.username}</span><Icon name="chevron" size={14} />
          </button>
          {accountOpen && <div id="account-options" role="menu" aria-label="Account" className="account-popover" onKeyDown={menuKeys}>
            <div className="account-identity"><Avatar user={user} decorative /><div><strong>{user.display_name || user.username}</strong><span className="muted">@{user.username}</span></div></div>
            <NavLink role="menuitem" to="/profile" className="account-menu-item"><Icon name="grid" size={16} /> My Profile</NavLink>
            <NavLink role="menuitem" to="/saved" className="account-menu-item"><Icon name="layers" size={16} /> Saved Content</NavLink>
            <NavLink role="menuitem" to="/settings" className="account-menu-item"><Icon name="key" size={16} /> Account Settings</NavLink>
            {creator && <><NavLink role="menuitem" to="/creator" className="account-menu-item"><Icon name="terminal" size={16} /> My rooms</NavLink><NavLink role="menuitem" to="/paths/mine" className="account-menu-item"><Icon name="compass" size={16} /> My learning paths</NavLink><NavLink role="menuitem" to="/creator/analytics" className="account-menu-item"><Icon name="grid" size={16} /> Creator Analytics</NavLink></>}
            <button type="button" role="menuitem" aria-label="Sign out" className="account-menu-item account-signout" onClick={() => { if (confirmUnsavedNavigation()) { setAccountOpen(false); logout() } }}><Icon name="logout" size={16} /> Sign Out</button>
          </div>}
        </div> : <><NavLink className="btn btn-ghost guest-login" to="/login">Log In</NavLink><NavLink className="btn btn-primary" to="/register">Sign Up</NavLink></>}
        <button type="button" ref={menuButton} className="btn btn-ghost icon-button mobile-menu-toggle" aria-label={mobileOpen ? 'Close navigation' : 'Open navigation'} aria-expanded={mobileOpen} aria-controls="main-navigation" onClick={() => { setMobileOpen(!mobileOpen); setAccountOpen(false) }}><Icon name={mobileOpen ? 'close' : 'menu'} /></button>
      </div>
    </div></header>
  </>
}
