import Icon from './Icon'
const copy: Record<string, [string, string]> = {
  pending: ['Queued', 'Your request was accepted. Provisioning has not completed yet.'],
  provisioning: ['Preparing your environment', 'Provisioning is in progress. Status refreshes automatically; this can take a few minutes.'],
  running: ['Ready to connect', 'Your environment is running. Connect your VPN to reach its private address.'],
  stopping: ['Cleanup in progress', 'Stop was requested. This state remains visible until resource cleanup is verified.'],
  stopped: ['Environment stopped', 'Cleanup is complete. Start a fresh environment when you are ready.'],
  failed: ['Action needed', 'An operation failed. Refresh status before retrying; resources may still need cleanup.'],
  expired: ['Time expired', 'VPN access is being revoked and automatic cleanup is pending.'],
}
export default function EnvironmentStatus({ status }: { status: string }) {
  const content = copy[status]
  if (!content) return null
  const waiting = ['pending', 'provisioning', 'stopping'].includes(status)
  return <div className={`environment-status environment-status-${status}`} role="status">
    <div className="environment-status-icon">{waiting ? <span className="spinner" aria-hidden="true" /> : <Icon name={status === 'running' || status === 'stopped' ? 'check' : 'refresh'} size={20} />}</div>
    <div><strong>{content[0]}</strong><p>{content[1]}</p></div>
  </div>
}
