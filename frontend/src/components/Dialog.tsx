import { useEffect, useId, useRef, type ReactNode } from 'react'
import Icon from './Icon'

// Native modal semantics provide a focus trap, Escape handling and an inert background.
export default function Dialog({ title, children, onClose, busy = false }: {
  title: string; children: ReactNode; onClose: () => void; busy?: boolean
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    const dialog = ref.current
    dialog?.showModal()
    const initial = dialog?.querySelector<HTMLElement>('[data-dialog-initial-focus]')
      ?? dialog?.querySelector<HTMLElement>('input:not([disabled]), textarea:not([disabled])')
    initial?.focus()
    return () => { dialog?.close(); previous?.focus() }
  }, [])
  return <dialog ref={ref} className="modal" aria-labelledby={titleId}
    onKeyDown={event => {
      if (event.key !== 'Tab') return
      const controls = Array.from(ref.current?.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), a[href], [tabindex="0"]') ?? [])
        .filter(control => control.tabIndex >= 0 && control.getClientRects().length > 0)
      const target = event.shiftKey && document.activeElement === controls[0]
        ? controls[controls.length - 1]
        : !event.shiftKey && document.activeElement === controls[controls.length - 1] ? controls[0] : null
      if (target) { event.preventDefault(); target.focus() }
    }}
    onCancel={event => { event.preventDefault(); if (!busy) onClose() }}>
    <header className="modal-header"><h2 className="modal-title" id={titleId}>{title}</h2>
      <button type="button" className="modal-close" onClick={onClose} disabled={busy} aria-label="Close dialog"><Icon name="close" /></button>
    </header>{children}
  </dialog>
}
