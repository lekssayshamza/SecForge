import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import api from '../api/axios'
import { getAuthOptions, linkGoogle } from '../api/auth'
import PasswordField from './PasswordField'
import { actionError } from '../lib/presentation'
export default function SignInMethods() {
  const options = useQuery({ queryKey: ['auth-options'], queryFn: getAuthOptions, staleTime: 60_000 })
  const methods = useQuery({ queryKey: ['auth-methods'], queryFn: async () => (await api.get<{ password_enabled: boolean; google_linked: boolean }>('/auth/methods')).data })
  const [password, setPassword] = useState(''), [pending, setPending] = useState(false), [error, setError] = useState('')
  const submit = async (e: React.FormEvent) => {
    e.preventDefault(); if (pending) return; setPending(true); setError('')
    try { const result = await linkGoogle(password); setPassword(''); window.location.assign(result.authorization_url) }
    catch (err: any) { setError(err?.response?.status === 401 ? 'Current password is incorrect. Please try again.' : actionError(err, 'link Google')); setPending(false) }
  }
  return <section className="panel signin-methods" aria-labelledby="signin-methods-title"><h2 id="signin-methods-title">Sign-in methods</h2>
    {methods.isPending ? <p role="status">Loading sign-in methods…</p> : methods.isError ? <p className="error" role="alert">Sign-in methods are unavailable. Refresh to retry.</p> : <>
      {methods.data.password_enabled && <p><Link to="/password">Change password</Link></p>}
      {methods.data.google_linked ? <p className="form-hint">Google is connected to your account.</p> : options.data?.google_enabled && methods.data.password_enabled &&
      <form onSubmit={submit}>{error && <div className="error" role="alert">{error}</div>}<p className="form-hint">Confirm your current password, then select your Google account. Email matches never link accounts automatically.</p>
        <PasswordField id="link-password" label="Current password" value={password} onChange={setPassword} disabled={pending} />
        <button type="submit" className="btn btn-secondary" disabled={pending}>{pending ? 'Connecting…' : 'Link Google account'}</button>
      </form>}
    </>}
  </section>
}
