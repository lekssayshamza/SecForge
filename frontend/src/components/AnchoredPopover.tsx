import { useEffect, useId, useRef, useState, type ReactNode } from 'react'

// A nonmodal disclosure: Tab can leave it, Escape returns to its trigger, and
// outside pointer/focus interactions dismiss it without stealing focus.
export default function AnchoredPopover({ label, accessibleLabel, className = '', buttonClassName = '', children }: {
  label: ReactNode; accessibleLabel: string; className?: string; buttonClassName?: string
  children: (close: () => void) => ReactNode
}) {
  const [open, setOpen] = useState(false), id = useId()
  const root = useRef<HTMLDivElement>(null), trigger = useRef<HTMLButtonElement>(null), panel = useRef<HTMLDivElement>(null)
  const close = () => { setOpen(false); trigger.current?.focus() }
  useEffect(() => {
    if (!open) return
    const dismiss = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false) }
    const place = () => {
      if (!root.current || !panel.current) return
      const rect = root.current.getBoundingClientRect(), below = window.innerHeight - rect.bottom - 14, above = rect.top - 14
      const upwards = below < 260 && above > below
      panel.current.style.top = upwards ? 'auto' : 'calc(100% + 6px)'
      panel.current.style.bottom = upwards ? 'calc(100% + 6px)' : 'auto'
      panel.current.style.maxHeight = `${Math.max(0, Math.min(480, upwards ? above : below))}px`
    }
    document.addEventListener('pointerdown', dismiss)
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    place()
    const first = panel.current?.querySelector<HTMLElement>('input, button, [tabindex="0"]')
    ;(first ?? panel.current)?.focus()
    return () => {
      document.removeEventListener('pointerdown', dismiss)
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [open])
  return <div className={`anchored-popover ${className}`} ref={root}
    onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false) }}
    onKeyDown={event => { if (open && event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close() } }}>
    <button type="button" ref={trigger} className={buttonClassName} aria-label={accessibleLabel} aria-haspopup="dialog" aria-expanded={open} aria-controls={open ? id : undefined}
      onClick={() => setOpen(!open)} onKeyDown={event => { if (event.key === 'ArrowDown') { event.preventDefault(); setOpen(true) } }}>{label}</button>
    {open && <div ref={panel} id={id} className="anchored-panel" role="dialog" aria-label={accessibleLabel} tabIndex={-1}>{children(close)}</div>}
  </div>
}
