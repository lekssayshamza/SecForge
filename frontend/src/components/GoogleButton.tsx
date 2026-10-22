import { useQuery } from '@tanstack/react-query'
import { getAuthOptions, googleStartUrl } from '../api/auth'
import { rememberReturnTo } from '../lib/returnTo'
export default function GoogleButton({ disabled = false, returnTo = '/' }: { disabled?: boolean; returnTo?: string }) {
  const { data, isPending, isError } = useQuery({ queryKey: ['auth-options'], queryFn: getAuthOptions, staleTime: 60_000, retry: false })
  return <div className="google-signin"><button type="button" className="btn btn-secondary btn-full" disabled={disabled || !data?.google_enabled}
    onClick={() => { rememberReturnTo(returnTo); window.location.assign(googleStartUrl()) }}><span className="google-mark" aria-hidden="true">G</span> Continue with Google</button>
    {isPending ? <p className="form-hint" role="status">Checking sign-in options…</p> : !data?.google_enabled && <p className="form-hint">{isError ? 'Google sign-in is temporarily unavailable.' : 'Google sign-in is not enabled yet.'}</p>}
    <div className="auth-divider"><span>or use your account</span></div></div>
}
