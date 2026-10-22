import { useState } from 'react'
import { Link, useNavigate, useLocation } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import AuthArtwork from '../components/AuthArtwork'
import GoogleButton from '../components/GoogleButton'
import PasswordField from '../components/PasswordField'
import { actionError } from '../lib/presentation'
import { authDestination, authLink, rememberReturnTo, consumeReturnTo } from '../lib/returnTo'
export default function Register() {
  const { register } = useAuth(), navigate = useNavigate()
  const destination = authDestination(useLocation().search)
  const [username, setUsername] = useState(''), [email, setEmail] = useState(''), [password, setPassword] = useState(''), [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState(''), [pending, setPending] = useState(false)
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); if (pending) return; setError('')
    if (password !== confirmPassword) { setError('Passwords do not match'); return }
    if (password.length < 12) { setError('Use at least 12 characters'); return }
    setPending(true)
    try { rememberReturnTo(destination); await register(username, email, password); consumeReturnTo(); navigate(destination, { replace: true }) }
    catch (err: any) { setError(err?.response?.status === 409 ? 'That username or email is already in use. Choose another or sign in.' : err?.response?.status === 422 ? 'Check your email, username and password. Use a valid email and a 3–50 character username.' : actionError(err, 'create your account')) }
    finally { setPending(false) }
  }
  return <div className="auth-page auth-v2"><AuthArtwork /><main id="main-content" tabIndex={-1} className="auth-form-col"><div className="auth-form-wrap">
    <header className="auth-header"><h1 className="auth-title">Create account</h1><p className="auth-subtitle">Start learning with a SecForge account.</p></header><GoogleButton disabled={pending} returnTo={destination} />
    <form onSubmit={submit} className="auth-form">{error && <div className="error" role="alert">{error}</div>}
      <div className="form-group"><label htmlFor="username">Username</label><input id="username" value={username} onChange={e => setUsername(e.target.value)} required minLength={3} maxLength={50} pattern="[a-zA-Z0-9][a-zA-Z0-9._\-]{2,49}" autoComplete="username" autoFocus disabled={pending} aria-describedby="registration-username-help" /><p id="registration-username-help" className="form-hint">3–50 letters, numbers, dots, underscores or hyphens. Start with a letter or number; usernames are stored lowercase.</p></div>
      <div className="form-group"><label htmlFor="email">Email address</label><input id="email" type="email" value={email} onChange={e => setEmail(e.target.value)} required maxLength={255} autoComplete="email" disabled={pending} /></div>
      <PasswordField id="password" label="Password" value={password} onChange={setPassword} disabled={pending} newPassword />
      <p className="form-hint" id="password-help">Use 12–128 characters. A long, unique passphrase is best. Avoid names and reused passwords.</p>
      <PasswordField id="confirmPassword" label="Confirm password" value={confirmPassword} onChange={setConfirmPassword} disabled={pending} newPassword />
      <button type="submit" className="btn btn-primary btn-full" disabled={pending}>{pending ? 'Creating account…' : 'Create account'}</button>
    </form><p className="auth-footer">Already have an account? <Link to={authLink('/login', destination)}>Sign in</Link></p>
  </div></main></div>
}
