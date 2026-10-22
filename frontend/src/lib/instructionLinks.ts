export function safeInstructionLink(value: string): string | null {
  if (/^\/(?:rooms(?:\/[1-9][0-9]*)?|paths(?:\/[1-9][0-9]*)?|leaderboard|profile)$/.test(value)) return value
  if (/[\u0000-\u0020\u007f\\]/.test(value)) return null
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null } catch { return null }
}
