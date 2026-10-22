import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import AuthArtwork from '../components/AuthArtwork'
import PasswordField from '../components/PasswordField'
import { changePassword } from '../api/auth'
import { useAuth } from '../context/AuthContext'
import { actionError } from '../lib/presentation'
import { consumeReturnTo } from '../lib/returnTo'
export default function PasswordChange() {
  const { acceptToken, logout, user } = useAuth(), navigate = useNavigate()
  const [current, setCurrent] = useState(''), [next, setNext] = useState(''), [confirmation, setConfirmation] = useState('')
  const [pending, setPending] = useState(false), [error, setError] = useState('')
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); if (pending) return; setError('')
    if (next !== confirmation) { setError('Passwords do not match'); return }
    setPending(true)
    try { const result = await changePassword(current, next); await acceptToken(result.access_token); const destination = consumeReturnTo(); navigate(user?.must_change_password ? destination : '/settings', { replace: true }) }
    catch (err: any) { setError(err?.response?.status === 401 ? 'Current password is incorrect. Please try again.' : actionError(err, 'change your password')) }
    finally { setPending(false) }
  }
  return <div className="auth-page auth-v2"><AuthArtwork /><main id="main-content" tabIndex={-1} className="auth-form-col"><div className="auth-form-wrap"><h1 className="auth-title">Choose your password</h1>
    <p className="auth-subtitle">{user?.must_change_password ? 'Replace your temporary password before using SecForge.' : 'Changing your password signs out other sessions.'}</p>
    <form className="auth-form" onSubmit={submit}>{error && <div className="error" role="alert">{error}</div>}
      <PasswordField id="current-password" label="Current password" value={current} onChange={setCurrent} disabled={pending} />
      <PasswordField id="new-password" label="New password" value={next} onChange={setNext} disabled={pending} newPassword />
      <p className="form-hint">Use a unique passphrase with 12–128 characters.</p>
      <PasswordField id="confirm-password" label="Confirm password" value={confirmation} onChange={setConfirmation} disabled={pending} newPassword />
      <button type="submit" className="btn btn-primary btn-full" disabled={pending}>{pending ? 'Saving…' : 'Save password'}</button>
    </form>{user?.must_change_password ? <button className="btn btn-ghost btn-full" onClick={logout} disabled={pending}>Sign out</button> : <Link to="/settings" className="auth-return">Back to settings</Link>}
  </div></main></div>
}
