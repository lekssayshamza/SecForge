import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useAuth } from '../context/AuthContext'
import { hasCreatorAccess } from '../lib/permissions'
import { getCreatorKey, saveCreatorKey, removeCreatorKey, applyCreatorKey, adminCreatorKey, revokeCreatorKey } from '../api/creatorKeys'
import { actionError } from '../lib/presentation'
import { formatDateTime } from '../lib/format'
import { useUnsavedChanges } from '../hooks/useUnsavedChanges'
import { useConfirm } from './ConfirmProvider'
import type { Account } from '../api/operations'

export default function CreatorSshSettings() {
  const { user } = useAuth(), allowed = hasCreatorAccess(user), client = useQueryClient(), confirm = useConfirm()
  const query = useQuery({ queryKey: ['creator-key', user?.id], queryFn: getCreatorKey, enabled: allowed, retry: false })
  const [text, setText] = useState(''), [edited, setEdited] = useState(false), [pending, setPending] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('')
  useEffect(() => { if (!edited) setText(query.data?.public_key || '') }, [query.data?.public_key, edited])
  useUnsavedChanges(allowed && edited && text.trim() !== (query.data?.public_key || ''))
  if (!allowed) return null
  const key = query.data
  const act = async (remove = false) => {
    if (pending || !key) return
    if (remove && !await confirm({ title: 'Revoke your builder key?', message: 'This removes your personal key from your builder. Existing SSH sessions are not disconnected. Platform management access stays available.', label: 'Revoke key', danger: true })) return
    setPending(true); setError(''); setNotice('')
    try {
      const result = remove ? await removeCreatorKey(key.revision) : await saveCreatorKey(text.trim(), key.revision)
      client.setQueryData(['creator-key', user?.id], result); setEdited(false)
      setNotice(result.pending ? 'Saved. Guest verification is pending; retry from your builder when it is reachable.' : remove ? 'Personal builder key revoked.' : 'Public key saved. New builders use this key automatically.')
    } catch (err) { setError(actionError(err, remove ? 'revoke this key' : 'save this public key')) }
    finally { setPending(false) }
  }
  return <section id="creator-ssh" className="panel creator-key-settings"><h2>Personal builder SSH key</h2><p className="muted">Create a key on your computer and register its public half. Your private key stays with you.</p>
    <details><summary>Create a personal key</summary><pre><code>ssh-keygen -t ed25519 -f ~/.ssh/secforge_creator{`\n`}chmod 600 ~/.ssh/secforge_creator{`\n`}cat ~/.ssh/secforge_creator.pub</code></pre><p>Choose a passphrase when prompted. Paste only the .pub output below. Never upload your private key or share the platform management key.</p></details>
    {query.isPending ? <p role="status">Loading SSH settings…</p> : query.isError ? <p className="error" role="alert">SSH settings are unavailable. <button type="button" onClick={() => void query.refetch()}>Retry</button></p> : <form onSubmit={event => { event.preventDefault(); void act() }}>
      {key?.configured && <p><strong>{key.enabled ? 'Registered key' : 'Revoked key'}</strong> <code className="key-fingerprint">{key.fingerprint}</code></p>}
      {key?.pending && <p className="warning" role="status">A builder still needs an update. Revocation is not complete on an unreachable machine; retry while it is running.</p>}
      {error && <p className="error" role="alert">{error}</p>}{notice && <p className="form-hint" role="status">{notice}</p>}
      <label htmlFor="creator-public-key">OpenSSH public key</label><textarea id="creator-public-key" rows={4} maxLength={2048} value={text} disabled={pending} onChange={event => { setEdited(true); setText(event.target.value); setNotice('') }} placeholder="ssh-ed25519 AAAA…" aria-describedby="creator-key-help" />
      <p id="creator-key-help" className="form-hint">Ed25519 recommended; RSA requires at least 3072 bits. Only your authorized private builder receives access. Captured learner clones do not activate this key.</p>
      <div className="button-row"><button className="btn btn-primary" disabled={pending || !text.trim() || text.includes('PRIVATE KEY') || !key || (!key.enabled && text.trim() === key.public_key)}>{pending ? 'Updating…' : key?.enabled ? 'Save public key' : 'Register a new public key'}</button>{(key?.enabled || key?.pending) && <button type="button" className="btn btn-secondary" disabled={pending} onClick={() => void act(true)}>{key?.enabled ? 'Revoke key' : 'Retry guest revocation'}</button>}</div>
    </form>}
  </section>
}

export function BuilderCreatorKey({ roomId, builderId }: { roomId: number; builderId: number }) {
  const { user } = useAuth(), client = useQueryClient(), [pending, setPending] = useState(false), [error, setError] = useState('')
  const query = useQuery({ queryKey: ['creator-key', user?.id], queryFn: getCreatorKey, enabled: hasCreatorAccess(user), retry: false, staleTime: 30000 })
  const key = query.data
  const apply = async () => {
    if (pending) return
    setPending(true); setError('')
    try { client.setQueryData(['creator-key', user?.id], await applyCreatorKey(roomId)) }
    catch (err) { setError(actionError(err, 'apply your builder key')); void query.refetch() }
    finally { setPending(false) }
  }
  return <section className="builder-personal-key"><h3>Your builder access</h3>{query.isPending ? <p role="status">Checking personal key…</p> : query.isError ? <p className="error" role="alert">Could not read key settings. <button type="button" onClick={() => void query.refetch()}>Retry</button></p> : !key?.enabled ? <p><Link to="/settings#creator-ssh">Register a personal public key</Link> to connect without receiving a shared private key.</p> : <><code className="key-fingerprint">{key.fingerprint}</code><p className="form-hint">{!key.pending && key.applied_builder_id === builderId && key.applied_at ? `Last applied ${formatDateTime(key.applied_at)}. The instance-bound key restores after reboot.` : 'Apply your registered key before connecting. Existing administrative access is preserved.'}</p><button type="button" className="btn btn-secondary btn-sm" disabled={pending} onClick={() => void apply()}>{pending ? 'Verifying SSH key…' : 'Apply my SSH key'}</button></>}{error && <p role="alert" className="error">{error}</p>}</section>
}

export function AdminCreatorKey({ target }: { target: Account }) {
  const { user } = useAuth(), client = useQueryClient(), [confirmation, setConfirmation] = useState(''), [reason, setReason] = useState('owner-request'), [pending, setPending] = useState(false), [error, setError] = useState('')
  const allowed = user?.role === 'admin' && ['creator', 'admin'].includes(target.role)
  const query = useQuery({ queryKey: ['admin-creator-key', target.id], queryFn: () => adminCreatorKey(target.id), enabled: allowed, retry: false })
  if (!allowed) return null
  const key = query.data
  const revoke = async () => {
    if (pending || !key || confirmation !== target.username) return
    setPending(true); setError('')
    try { const result = await revokeCreatorKey(target.id, key.revision, confirmation, reason); client.setQueryData(['admin-creator-key', target.id], result); void client.invalidateQueries({ queryKey: ['creator-key'] }); setConfirmation('') }
    catch (err) { setError(actionError(err, 'revoke this creator key')) }
    finally { setPending(false) }
  }
  return <section className="admin-key-access"><h3>Builder SSH access</h3>{query.isPending ? <p role="status">Loading key status…</p> : query.isError ? <p className="error" role="alert">Key status unavailable.</p> : !key?.configured ? <p className="muted">No personal key registered.</p> : <><p>{key.enabled ? 'Registered' : 'Revoked'} · <code className="key-fingerprint">{key.fingerprint}</code></p>{key.pending && <p className="warning">Guest removal is pending. Retry when the builder is reachable; do not assume an unreachable guest is already revoked.</p>}{(key.enabled || key.pending) && <form onSubmit={event => { event.preventDefault(); void revoke() }}><label>Reason<select value={reason} onChange={e => setReason(e.target.value)} disabled={pending}><option value="owner-request">Owner request</option><option value="access-change">Access change</option><option value="account-review">Account review</option></select></label><label>Type {target.username} to confirm<input value={confirmation} onChange={e => setConfirmation(e.target.value)} disabled={pending} autoComplete="off" /></label><p className="form-hint">Revokes the personal key for new SSH logins. Does not disconnect existing SSH sessions or change the shared management key.</p><button type="submit" className="btn btn-danger btn-sm" disabled={pending || confirmation !== target.username}>{pending ? 'Revoking…' : key.enabled ? 'Revoke registered key' : 'Retry guest revocation'}</button></form>}</>}{error && <p className="error" role="alert">{error}</p>}</section>
}
