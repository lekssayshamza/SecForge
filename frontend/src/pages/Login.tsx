import { useState } from 'react'
import { Link, useNavigate, useLocation } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import AuthArtwork from '../components/AuthArtwork'
import PasswordField from '../components/PasswordField'
import GoogleButton from '../components/GoogleButton'
import { actionError } from '../lib/presentation'
import { authDestination, authLink, rememberReturnTo, consumeReturnTo } from '../lib/returnTo'
const oauthErrors: Record<string, string> = { cancelled: 'Google sign-in was cancelled. You can try again.', invalid: 'That sign-in attempt expired or could not be verified. Please start again.', provider: 'Google sign-in is temporarily unavailable. Please try again.' }
export default function Login() {
  const { login } = useAuth(), navigate = useNavigate(), location = useLocation()
  const [username, setUsername] = useState(''), [password, setPassword] = useState('')
  const [error, setError] = useState(oauthErrors[new URLSearchParams(location.search).get('oauth') || ''] || ''), [pending, setPending] = useState(false)
  const requested = (location.state as { from?: { pathname?: string } })?.from?.pathname
  const from = authDestination(location.search, requested)
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); if (pending) return; setError(''); setPending(true)
    try { rememberReturnTo(from); const user = await login(username, password); if (!user.must_change_password) consumeReturnTo(); navigate(user.must_change_password ? '/password' : from, { replace: true }) }
    catch (err: any) { setError(err?.response?.status === 401 ? 'Username/email or password is incorrect. Please try again.' : actionError(err, 'sign in')) }
    finally { setPending(false) }
  }
  return <div className="auth-page auth-v2"><AuthArtwork /><main id="main-content" tabIndex={-1} className="auth-form-col"><div className="auth-form-wrap">
    <header className="auth-header"><h1 className="auth-title">Welcome back</h1><p className="auth-subtitle">Continue your next challenge.</p></header><GoogleButton disabled={pending} returnTo={from} />
    <form onSubmit={submit} className="auth-form">{error && <div className="error" role="alert">{error}</div>}
      <div className="form-group"><label htmlFor="username">Username or email</label><input id="username" value={username} onChange={e => setUsername(e.target.value)} required maxLength={255} autoComplete="username" autoFocus disabled={pending} /></div>
      <PasswordField id="password" label="Password" value={password} onChange={setPassword} disabled={pending} />
      <button type="submit" className="btn btn-primary btn-full" disabled={pending}>{pending ? 'Signing in…' : 'Sign in'}</button>
    </form><p className="auth-footer">No account? <Link to={authLink('/register', from)}>Create one</Link></p>
  </div></main></div>
}
