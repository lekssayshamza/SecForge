import { useState } from 'react'
import type { DraftEditor } from '../api/workspace'
import { defaultPorts, parsePortInputs, type PortInput } from '../lib/networkPorts'
import { useUnsavedChanges, confirmDiscard } from '../hooks/useUnsavedChanges'

export default function NetworkPorts({ draft }: { draft: DraftEditor }) {
  const ports = draft.data.room.exposed_ports ?? defaultPorts
  const [editing, setEditing] = useState(false)
  const [rows, setRows] = useState<PortInput[]>([])
  const [revision, setRevision] = useState(0)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  useUnsavedChanges(editing)
  const begin = () => {
    setRows(ports.map(rule => ({ protocol: rule.protocol, start: String(rule.from_port), end: rule.to_port === rule.from_port ? '' : String(rule.to_port) })))
    setRevision(draft.data.revision); setError(null); setEditing(true); setSaved(false)
  }
  const save = async (event: React.FormEvent) => {
    event.preventDefault(); setError(null)
    let exposed_ports
    try { exposed_ports = parsePortInputs(rows) } catch (failure) { setError((failure as Error).message); return }
    setPending(true)
    try {
      await draft.write('metadata', { exposed_ports }, revision)
      setEditing(false); setSaved(true)
    } catch (failure: any) {
      setError(failure?.response?.status === 409 ? 'The draft changed. Cancel and reopen this editor to review the latest ports.' : 'Unable to save ports. Check your rules and try again.')
    } finally { setPending(false) }
  }
  const change = (index: number, values: Partial<PortInput>) => setRows(rows.map((row, i) => i === index ? { ...row, ...values } : row))
  return <section className="panel" id="network-ports" aria-labelledby="network-ports-title">
    <div className="briefing-heading"><h2 className="panel-title" id="network-ports-title">Network &amp; Exposed Ports</h2>
      {!editing && <button type="button" className="btn btn-ghost btn-sm" onClick={begin}>Edit ports</button>}</div>
    <p className="form-hint">Accessible through the SecForge VPN.</p>
    {!editing && <>{ports.length ? <ul className="network-port-badges">{ports.map(rule => <li className="meta-pill" key={`${rule.protocol}-${rule.from_port}-${rule.to_port}`}>
      {rule.protocol.toUpperCase()} {rule.from_port}{rule.to_port !== rule.from_port && `–${rule.to_port}`}</li>)}</ul> : <p>No additional service ports configured.</p>}
      {saved && <p className="form-hint" role="status">Ports saved.</p>}</>}
    {editing && <form onSubmit={event => void save(event)}>
      <fieldset disabled={pending} className="modal-fields">
        <legend>Service ports</legend>
        <p className="form-hint">Up to 20 rules and 4,096 ports. Leave the end blank for a single port.</p>
        {rows.map((row, index) => <div className="network-port-row" key={index}>
          <div className="form-group"><label htmlFor={`port-protocol-${index}`}>Protocol {index + 1}</label><select id={`port-protocol-${index}`} value={row.protocol} onChange={e => change(index, { protocol: e.target.value as PortInput['protocol'] })}><option value="tcp">TCP</option><option value="udp">UDP</option></select></div>
          <div className="form-group"><label htmlFor={`port-start-${index}`}>Start port</label><input id={`port-start-${index}`} inputMode="numeric" pattern="[0-9]+" required maxLength={5} value={row.start} onChange={e => change(index, { start: e.target.value })} /></div>
          <div className="form-group"><label htmlFor={`port-end-${index}`}>End port (optional)</label><input id={`port-end-${index}`} inputMode="numeric" pattern="[0-9]*" maxLength={5} value={row.end} onChange={e => change(index, { end: e.target.value })} /></div>
          <button type="button" className="btn btn-ghost btn-sm" aria-label={`Remove port rule ${index + 1}`} onClick={() => setRows(rows.filter((_, i) => i !== index))}>Remove</button>
        </div>)}
        <button type="button" className="btn btn-secondary btn-sm" disabled={rows.length >= 20} onClick={() => setRows([...rows, { protocol: 'tcp', start: '', end: '' }])}>Add port rule</button>
        {error && <p className="error" role="alert">{error}</p>}
        <div className="form-row"><button type="submit" className="btn btn-primary">{pending ? 'Saving…' : 'Save ports'}</button><button type="button" className="btn btn-secondary" onClick={() => { if (confirmDiscard(true)) setEditing(false) }}>Cancel</button></div>
      </fieldset>
    </form>}
  </section>
}
