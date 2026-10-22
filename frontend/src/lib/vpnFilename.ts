export function vpnProfileFilename(username: string): string {
  const name = username.toLowerCase().replace(/[^a-z0-9._-]/g, '-').replace(/^[._-]+|[._-]+$/g, '').slice(0, 50) || 'user'
  return `secforge-${name}.ovpn`
}

export function safeProfileFilename(disposition: string | null | undefined, username: string): string {
  if (disposition) {
    const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition)
    if (match) {
      try {
        const name = decodeURIComponent(match[1])
        if (/^secforge-[a-z0-9._-]{1,50}\.ovpn$/.test(name)) return name
      } catch { /* malformed server header: use the current account name */ }
    }
  }
  return vpnProfileFilename(username)
}
