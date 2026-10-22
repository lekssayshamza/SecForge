import type { Lab } from '../types/api'

export function stampLabTime(lab: Lab): Lab {
  return { ...lab, clock_received_at: performance.now() }
}

export function remainingSeconds(lab: Lab | null | undefined, monotonicNow: number): number | null {
  if (!lab?.server_time || lab.clock_received_at === undefined) return null
  const duration = Date.parse(lab.expires_at) - Date.parse(lab.server_time)
  if (!Number.isFinite(duration)) return null
  return Math.max(0, Math.ceil((duration - Math.max(0, monotonicNow - lab.clock_received_at)) / 1000))
}

export function formatCountdown(seconds: number): string {
  const value = Math.max(0, Math.floor(seconds))
  return [Math.floor(value / 3600), Math.floor(value % 3600 / 60), value % 60].map(part => String(part).padStart(2, '0')).join(':')
}

export function canExtendLab(lab: Lab, seconds: number | null): boolean {
  return !!lab.session_started_at && !lab.expired_at && lab.status === 'running' && seconds !== null && seconds > 0 && seconds <= 1800 && (lab.extensions_used ?? 0) < 2
}
