import { useState, type ReactNode } from 'react'
import Icon from './Icon'

export default function ConnectionGuide({ address, builder = false, sshUsername, profileFilename = 'secforge.ovpn', download, compact = false }: { address: string; builder?: boolean; sshUsername?: string; profileFilename?: string; download: ReactNode; compact?: boolean }) {
  const [copyStatus, setCopyStatus] = useState('')
  // Copy a shell command only for validated connection metadata. Do not infer
  // an admin login if the backend has not supplied its configured username.
  const validUsername = sshUsername && /^[a-z_][a-z0-9_-]*$/i.test(sshUsername)
  const validAddress = address.split('.').length === 4 && address.split('.').every(part => /^\d{1,3}$/.test(part) && Number(part) <= 255)
  const sshCommand = validUsername && validAddress ? `ssh -i ~/sf/CyberRange-Key.pem ${sshUsername}@${address}` : null
  // Download names are data, never unquoted shell syntax. Include a relative
  // path so a filename cannot be interpreted as a command-line option.
  const profilePath = `./${profileFilename}`.replace(/'/g, `'"'"'`)
  const vpnCommand = `sudo openvpn --config '${profilePath}'`
  const developmentVpnCommand = `sudo /usr/local/libexec/secforge/learner_vpn '${profilePath}'`
  const copy = async (value: string, label: string) => {
    try { await navigator.clipboard.writeText(value); setCopyStatus(`${label} copied`) }
    catch { setCopyStatus(`Copy unavailable. Select the ${label.toLowerCase()} to copy it.`) }
  }
  return <div className={`connection-guide${compact ? ' connection-guide-compact' : ''}`}>
    <div className="connection-address"><div><span className="eyebrow">Assigned private address</span><code>{address}</code></div>
      <button type="button" className="btn btn-secondary btn-sm" onClick={() => void copy(address, 'Address')} aria-label="Copy private address"><Icon name="copy" size={16} /> Copy</button></div>
    {copyStatus && <p className="form-hint" role="status">{copyStatus}</p>}
    <div className="connection-tools"><div className="profile-download">{download}</div><a className="btn btn-secondary btn-sm" href={`http://${address}`} target="_blank" rel="noopener noreferrer">Open HTTP challenge <Icon name="arrow" size={16} /></a></div>
    {!compact && <p className="form-hint">VPN required · Your client reports connection status; this website cannot verify your tunnel. HTTP requires a challenge service on port 80.</p>}
    {builder && <section className="builder-ssh" aria-labelledby="builder-ssh-title">
      <h3 id="builder-ssh-title">Connect to Ubuntu over SSH</h3>
      <p>Connect with your creator VPN profile first. Access is limited to your authorized private builder.</p>
      {sshCommand ? <>
        <p>SSH user: <code>{sshUsername}</code> · Port: <code>22</code> · Authentication: PEM key</p>
        <div className="builder-ssh-command"><div className="command-line"><Icon name="terminal" size={16} /><code>{sshCommand}</code></div><button type="button" className="btn btn-secondary btn-sm" onClick={() => void copy(sshCommand, 'SSH command')} aria-label="Copy builder SSH command"><Icon name="copy" size={16} /> Copy command</button></div>
      </> : <p className="error" role="alert">SSH connection details are unavailable. Refresh builder status; if this persists, ask the administrator to check the configured SSH username.</p>}
      <p className="form-hint">Use your existing CyberRange-Key.pem file. The shown path is local to your computer; replace it if you keep the file elsewhere. Keep the private file on your computer with permissions 400 or 600. SecForge never downloads or displays this key. Your VPN profile is not an SSH key.</p>
      <p className="form-hint">Connect the creator VPN before running SSH. A connected management VPN alone does not authorize creator access. If the command times out, check that the route uses your creator tunnel using the instructions below.</p>
    </section>}
    <details className="connection-help"><summary>VPN &amp; SSH instructions</summary>
    <ol className="connection-steps">
      <li><strong>Download your VPN profile</strong><p>Keep your profile private; it contains your VPN identity and is reusable for your assigned labs.</p></li>
      <li><strong>Connect with OpenVPN</strong><p>With just one SecForge VPN, import your profile in a VPN client or run <code>{vpnCommand}</code> from the download folder. Wait for the connection to complete.</p></li>
      <li><strong>Backend management VPN also running?</strong><p>Keep the management VPN connected. Disconnect only your previous user VPN, then use the installed development launcher from the download folder:</p>
        <div className="builder-ssh-command"><div className="command-line"><Icon name="terminal" size={16} /><code>{developmentVpnCommand}</code></div><button type="button" className="btn btn-secondary btn-sm" onClick={() => void copy(developmentVpnCommand, 'Development VPN command')} aria-label="Copy development VPN command"><Icon name="copy" size={16} /> Copy</button></div>
        <p className="form-hint">Your downloaded profile must have permissions 600. Use the profile for the account that owns this machine. Running ordinary OpenVPN alongside the management VPN can select the wrong tunnel even when both connections succeed. The launcher selects your {builder ? 'creator' : 'learner'} tunnel automatically after reconnects. If it is not installed, ask the administrator to follow DUAL_VPN_ROUTING.md. Use one user VPN at a time.</p>
      </li>
      <li><strong>{builder ? 'Configure your challenge' : 'Open your challenge'}</strong><p>{builder ? 'Use the key-based command above. Restrict the key file to your user with chmod 600 before connecting, and verify the host-key fingerprint with the administrator on first connection. Make services start on boot before saving an image.' : 'Replace ROOM_USERNAME with the user named in the room briefing. Use the key or login configured by the creator.'}</p>
        {!builder && <div className="command-line"><Icon name="terminal" size={16} /><code>ssh ROOM_USERNAME@{address}</code></div>}
      </li>
      <li><strong>Connection troubleshooting</strong><p>A timeout usually means the VPN, private route, authorization or SSH service is not ready. Permission denied (publickey) means you should check the username and authorized key. A host-key warning requires administrator verification; do not disable host-key checks.</p><p>Verify the route to this address with <code>ip route get {address}</code>, then test <code>ping -c 3 {address}</code> where ICMP is permitted. A failed ping alone does not prove SSH is blocked. HTTP connection refused can mean no service is listening, not a VPN failure. If two SecForge VPNs run on one host, use the development launcher described above; do not replace routes manually.</p></li>
    </ol></details>
  </div>
}
