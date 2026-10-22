import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import AuthArtwork from '../components/AuthArtwork'
import { getGoogleStatus, completeGoogle } from '../api/auth'
import { useAuth } from '../context/AuthContext'
import { actionError } from '../lib/presentation'
import { consumeReturnTo } from '../lib/returnTo'
export default function GoogleComplete() {
  const { acceptToken } = useAuth(), navigate = useNavigate()
  const { data, isPending, isFetchedAfterMount, isFetching, error: loadError } = useQuery({ queryKey: ['google-handoff'], queryFn: getGoogleStatus, retry: false, staleTime: 0, refetchOnWindowFocus: false, refetchOnMount: 'always', gcTime: 0 })
  const [username, setUsername] = useState(''), [pending, setPending] = useState(false), [error, setError] = useState('')
  const operation = useRef(false), autoStarted = useRef(false)
  const finish = useCallback(async (chosenUsername?: string) => {
    if (operation.current) return
    operation.current = true; setPending(true); setError('')
    try {
      // The same browser-bound, origin-checked, single-use server POST remains
      // authoritative. Linking and first-time username selection stay explicit.
      const result = await completeGoogle(chosenUsername)
      const user = await acceptToken(result.access_token)
      if (user.must_change_password) navigate('/password', { replace: true })
      else { const destination = consumeReturnTo(); navigate(data?.linking ? '/settings' : destination, { replace: true }) }
    } catch (err: any) {
      setError(err?.response?.status === 409 ? err.response.data.detail : actionError(err, 'complete Google sign-in'))
    } finally { operation.current = false; setPending(false) }
  }, [acceptToken, navigate, data?.linking])
  useEffect(() => {
    // Never infer sign-in versus linking from a previously cached handoff.
    if (!isFetchedAfterMount || isFetching || loadError || !data || data.choose_username || data.linking || autoStarted.current) return
    autoStarted.current = true
    void finish()
  }, [data, finish, isFetchedAfterMount, isFetching, loadError])
  return <div className="auth-page auth-v2"><AuthArtwork /><main id="main-content" tabIndex={-1} className="auth-form-col"><div className="auth-form-wrap"><h1 className="auth-title">{data?.choose_username ? 'Make it yours' : data?.linking ? 'Link your Google account' : 'Signing you in'}</h1>
    {data?.choose_username && <p className="auth-subtitle">Choose your unique SecForge username.</p>}
    {data?.linking && <p className="auth-subtitle">Confirm that you want to link the Google account you selected.</p>}
    {isPending || (!isFetchedAfterMount && !loadError) || isFetching ? <p role="status">Verifying sign-in…</p> : loadError ? <div className="error" role="alert">Your sign-in attempt expired or could not be verified. <Link to="/login">Start again</Link></div> : <>
      {error && <div className="error" role="alert">{error} <Link to="/login">Start again</Link></div>}
      {data?.choose_username || data?.linking ? <form className="auth-form" onSubmit={event => { event.preventDefault(); void finish(data?.choose_username ? username : undefined) }}>
        {data?.choose_username && <div className="form-group"><label htmlFor="google-username">Username</label><input id="google-username" value={username} onChange={e => setUsername(e.target.value)} required minLength={3} maxLength={50} pattern="[a-zA-Z0-9][a-zA-Z0-9._\-]{2,49}" autoComplete="username" autoFocus disabled={pending} /><p className="form-hint">3–50 letters, numbers, dots, underscores or hyphens. No separate password is needed.</p></div>}
        <button type="submit" className="btn btn-primary btn-full" disabled={pending}>{pending ? 'Continuing…' : data?.linking ? 'Confirm link' : 'Create account'}</button>
      </form> : !error && <p role="status"><span className="spinner" aria-hidden="true" /> Completing secure sign-in…</p>}
    </>}
    <Link to="/login" className="auth-return">Back to sign in</Link>
  </div></main></div>
}
