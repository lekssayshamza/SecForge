import { useEffect, useRef, useState } from 'react'
import { canExtendLab, formatCountdown, remainingSeconds } from '../lib/labClock'
import type { Lab } from '../types/api'

export function useLabClock(lab: Lab | null | undefined) {
  const [tick, setTick] = useState(() => performance.now())
  useEffect(() => {
    if (!lab?.server_time || ['stopped', 'failed', 'stopping'].includes(lab.status)) return
    let timer: number | undefined
    const refresh = () => {
      if (timer !== undefined) window.clearInterval(timer)
      setTick(performance.now())
      if (document.visibilityState === 'visible') timer = window.setInterval(() => setTick(performance.now()), 1000)
    }
    refresh(); document.addEventListener('visibilitychange', refresh)
    return () => { if (timer !== undefined) window.clearInterval(timer); document.removeEventListener('visibilitychange', refresh) }
  }, [lab?.id, lab?.status, lab?.server_time])
  return remainingSeconds(lab, Math.max(tick, performance.now()))
}

export default function LabTimer({ lab, seconds, onExtend, pending = false, compact = false }: {
  lab: Lab; seconds: number | null; onExtend?: () => Promise<Lab>; pending?: boolean; compact?: boolean
}) {
  const [error, setError] = useState(''), [notice, setNotice] = useState('')
  const guard = useRef(false)
  const ended = ['stopped', 'failed', 'stopping'].includes(lab.status)
  const expired = !!lab.expired_at || (seconds === 0 && !['stopped', 'failed'].includes(lab.status))
  const warning = !ended && seconds !== null && seconds > 0 ? seconds <= 300 ? '5 minutes or less remain.' : seconds <= 600 ? '10 minutes or less remain.' : '' : ''
  const eligible = canExtendLab(lab, seconds)
  const extend = async () => {
    if (!onExtend || guard.current || !eligible) return
    guard.current = true; setError(''); setNotice('')
    try { await onExtend(); setNotice('Added 1 hour to your lab.') }
    catch (failure) {
      const status = (failure as { response?: { status?: number } })?.response?.status
      setError(status === 409 ? 'The lab is no longer eligible. Refresh its status and remaining time.' : status === 503 ? 'Time service unavailable. Refresh status before retrying.' : 'Could not extend the lab. Refresh status before retrying; a completed request will not add time twice.')
    } finally { guard.current = false }
  }
  return <div className={`lab-timer${warning || expired ? ' lab-timer-warning' : ''}`}>
    <div><span className="muted">{expired ? 'Expired' : ended ? 'Session ended' : 'Time remaining'}</span><strong role="timer" aria-live="off" aria-label="Lab time remaining">{seconds === null ? 'Syncing time…' : formatCountdown(ended ? 0 : seconds)}</strong></div>
    <div className="lab-timer-actions"><span>{Math.max(0, 2 - (lab.extensions_used ?? 0))} extensions remaining</span>{eligible && <button type="button" className="btn btn-secondary btn-sm" disabled={pending || !onExtend} onClick={() => void extend()}>{pending ? 'Extending…' : 'Extend +1 Hour'}</button>}</div>
    {warning && <p className="lab-time-message" role="status">{warning}</p>}
    {expired && <p className="lab-time-message" role="status">Access expired. {lab.status === 'stopped' ? 'Cleanup complete.' : 'Cleanup is automatic; you can leave this page.'}</p>}
    {!ended && !expired && !eligible && (lab.extensions_used ?? 0) < 2 && (compact ? <details className="context-help"><summary>Extension limits</summary><p>Extensions become available while running with 30 minutes or less remaining. Maximum session allocation: 3 hours.</p></details> : <p className="form-hint">Extensions become available while running with 30 minutes or less remaining. Maximum session allocation: 3 hours.</p>)}
    {error && <p className="error" role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
  </div>
}
