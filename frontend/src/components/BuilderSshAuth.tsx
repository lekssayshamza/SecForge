import { useRef, useState, type FormEvent } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import api from '../api/axios'
import type { Builder } from '../types/api'
import { actionError } from '../lib/presentation'
import { useUnsavedChanges } from '../hooks/useUnsavedChanges'

export default function BuilderSshAuth({ builder, onSaved }: { builder: Builder; onSaved: () => void }) {
  const [mode, setMode] = useState(builder.ssh_auth_mode ?? 'key'), [busy, setBusy] = useState(false)
  const [dirty, setDirty] = useState(false), [error, setError] = useState(''), [notice, setNotice] = useState('')
  const [createAccount, setCreateAccount] = useState(true)
  const operation = useRef(false), client = useQueryClient()
  useUnsavedChanges(dirty)
  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (operation.current) return
    const form = event.currentTarget, fields = new FormData(form)
    const data: {mode: string; username: string; password?: string; create_account: boolean} = { mode, username: String(fields.get('username')), create_account: mode === 'password' && createAccount }
    if (mode === 'password') data.password = String(fields.get('password'))
    operation.current = true; setBusy(true); setError(''); setNotice('')
    try {
      const result = await api.post<Builder>(`/rooms/${builder.room_id}/builder/ssh-auth`, data)
      client.setQueryData(['builder', builder.room_id], result.data)
      form.reset(); setDirty(false); setNotice('Guest SSH settings validated and saved. Capture the machine to preserve them.'); onSaved()
    } catch (err) { setError((err as {response?: {status?: number}})?.response?.status === 409
      ? 'SSH settings were not applied. Wait for the builder to finish starting, check the training account is unprivileged and account creation is selected if needed, and make sure no image capture is pending. Management key access is preserved.'
      : actionError(err, 'configure guest SSH')) }
    finally { delete data.password; operation.current = false; setBusy(false) }
  }
  return <section className="builder-auth" aria-labelledby="builder-training-access"><h3 id="builder-training-access">Training account SSH access</h3>
    <p>Management access uses your authorized SSH key. Key authentication is the recommended default.</p>
    <p>Password mode can create a separate, unprivileged training account automatically. For key mode, use an existing training account and install its authorized public key. Do not use your management account or add the training user to privileged groups.</p>
    <p className="form-hint">Password mode applies only to this training account from the authorized VPN network. It keeps key authentication enabled and persists in the captured image. Use a unique training password, never your SecForge password.</p>
    <form onInput={() => setDirty(true)} onSubmit={event => void save(event)}><fieldset disabled={busy} className="modal-fields">
      <label className="form-group" htmlFor="guest-ssh-mode">SSH authentication<select id="guest-ssh-mode" value={mode} onChange={event => setMode(event.target.value as 'key' | 'password')}><option value="key">SSH key (recommended)</option><option value="password">Training account password</option></select></label>
      <label className="form-group" htmlFor="guest-ssh-user">Training username<input id="guest-ssh-user" name="username" required maxLength={32} pattern="[a-z][a-z0-9_-]{0,31}" defaultValue={builder.ssh_guest_username ?? ''} autoComplete="off" /></label>
      {mode === 'password' && <label className="checkbox-label"><input type="checkbox" checked={createAccount} onChange={event => setCreateAccount(event.target.checked)} /> Create this unprivileged account if it does not exist</label>}
      {mode === 'password' && <label className="form-group" htmlFor="guest-ssh-password">New training password<input id="guest-ssh-password" name="password" type="password" required minLength={12} maxLength={128} autoComplete="new-password" /><span className="form-hint">12–128 characters. No colons or control characters. Applying a password enables this training account and clears account expiry. It is sent securely to the guest and is not saved in SecForge.</span></label>}
      <button className="btn btn-secondary btn-sm" disabled={busy}>{busy ? 'Validating guest settings…' : 'Apply guest SSH settings'}</button>
    </fieldset></form>
    {error && <p className="error" role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {builder.ssh_guest_username && <p>Configured: <strong>{builder.ssh_auth_mode === 'password' ? 'Password + key' : 'SSH key'}</strong> · <code>ssh {builder.ssh_guest_username}@{builder.private_ip}</code></p>}
  </section>
}
