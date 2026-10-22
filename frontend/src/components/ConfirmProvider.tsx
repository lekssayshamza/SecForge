import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import Dialog from './Dialog'
import { useLocation } from 'react-router-dom'
type Confirmation = { title: string; message: string; label?: string; danger?: boolean }
const Context = createContext<(options: Confirmation) => Promise<boolean>>(async () => false)
export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [options, setOptions] = useState<Confirmation | null>(null)
  const resolver = useRef<((accepted: boolean) => void) | null>(null)
  const location = useLocation()
  useEffect(() => () => { resolver.current?.(false) }, [])
  useEffect(() => {
    resolver.current?.(false)
    resolver.current = null
    setOptions(null)
  }, [location.pathname])
  const finish = (accepted: boolean) => { resolver.current?.(accepted); resolver.current = null; setOptions(null) }
  const confirm = (next: Confirmation) => {
    if (resolver.current) return Promise.resolve(false)
    setOptions(next)
    return new Promise<boolean>(resolve => { resolver.current = resolve })
  }
  return <Context.Provider value={confirm}>{children}{options && <Dialog title={options.title} onClose={() => finish(false)}>
    <div className="modal-body"><p className="dialog-copy">{options.message}</p></div>
    <footer className="modal-footer"><button type="button" className="btn btn-secondary" data-dialog-initial-focus onClick={() => finish(false)}>Cancel</button>
      <button type="button" className={`btn ${options.danger ? 'btn-danger' : 'btn-primary'}`} onClick={() => finish(true)}>{options.label || 'Confirm'}</button>
    </footer></Dialog>}</Context.Provider>
}
export const useConfirm = () => useContext(Context)
