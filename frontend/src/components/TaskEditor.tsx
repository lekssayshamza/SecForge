import { useRef, useState, type FormEvent } from 'react'
import type { DraftEditor } from '../api/workspace'
import { createTask, updateTask } from '../api/challenges'
import type { RoomTask } from '../types/api'
import Dialog from './Dialog'
import MarkdownEditor from './MarkdownEditor'
import { useUnsavedChanges, confirmDiscard } from '../hooks/useUnsavedChanges'
import { actionError } from '../lib/presentation'
export default function TaskEditor({ roomId, task, onClose, onSaved, draft }: {roomId: number; task: RoomTask | null; onClose: () => void; onSaved: () => Promise<void>; draft?: DraftEditor}) {
  const revision = useRef(draft?.data.revision)
  const [title, setTitle] = useState(task?.title ?? ''), [instructions, setInstructions] = useState(task?.instructions ?? '')
  const [pending, setPending] = useState(false), [error, setError] = useState(''), [imageBusy, setImageBusy] = useState(false), [saved, setSaved] = useState(false)
  const dirty = !saved && (title !== (task?.title ?? '') || instructions !== (task?.instructions ?? ''))
  useUnsavedChanges(dirty || imageBusy)
  const close = () => { if (!imageBusy && confirmDiscard(dirty)) onClose() }
  const save = async (event: FormEvent) => {
    event.preventDefault(); if (pending || imageBusy) return
    setPending(true); setError('')
    try { const data = { title: title.trim(), instructions }; if (draft) await draft.write(task ? 'task_update' : 'task_create', task ? { id: task.id, ...data } : data, revision.current); else if (task) await updateTask(roomId, task.id, data); else await createTask(roomId, data); setSaved(true); await onSaved() }
    catch (err) { setError(actionError(err, 'save this task')) }
    finally { setPending(false) }
  }
  return <Dialog title={task ? 'Edit task' : 'Add task'} onClose={close} busy={pending || imageBusy}><form onSubmit={event => void save(event)}><fieldset className="modal-body modal-fields" disabled={pending}>
    {error && <div className="error" role="alert">{error}</div>}<div className="form-group"><label htmlFor="task-title">Task title</label><input id="task-title" required maxLength={120} value={title} onChange={e => { setSaved(false); setTitle(e.target.value) }} /></div>
    <MarkdownEditor roomId={roomId} value={instructions} onChange={value => { setSaved(false); setInstructions(value) }} onBusy={setImageBusy} disabled={pending} />
    </fieldset><footer className="modal-footer"><button type="button" className="btn btn-secondary" onClick={close} disabled={pending || imageBusy}>Cancel</button><button type="submit" className="btn btn-primary" disabled={pending || imageBusy || !title.trim()}>{pending ? 'Saving…' : 'Save task'}</button></footer></form></Dialog>
}
