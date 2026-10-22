import { useEffect, useId, useRef, useState } from 'react'
/** Text-only disclosure: hover previews; click, tap or keyboard pins it open. */
export default function ContextHelp({ label, text }: { label: string; text: string }) {
  const [open, setOpen] = useState(false), id = useId(), root = useRef<HTMLSpanElement>(null), button = useRef<HTMLButtonElement>(null), pinned = useRef(false), popover = useRef<HTMLSpanElement>(null)
  const close = () => { pinned.current = false; setOpen(false) }
  useEffect(() => {
    if (!open) return
    const dismiss = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) { pinned.current = false; setOpen(false) } }
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { pinned.current = false; setOpen(false); button.current?.focus() } }
    const position = () => {
      const anchor = button.current?.getBoundingClientRect(), tip = popover.current
      if (!anchor || !tip) return
      const width = Math.min(260, window.innerWidth - 32)
      tip.style.position = 'fixed'; tip.style.width = `${width}px`; tip.style.insetInlineEnd = 'auto'
      tip.style.left = `${Math.max(16, Math.min(anchor.right - width, window.innerWidth - width - 16))}px`
      const height = tip.getBoundingClientRect().height
      tip.style.top = `${Math.max(8, anchor.bottom + height + 15 < window.innerHeight ? anchor.bottom + 7 : anchor.top - height - 7)}px`
    }
    position(); window.addEventListener('resize', position); window.addEventListener('scroll', position, true)
    document.addEventListener('pointerdown', dismiss); document.addEventListener('keydown', escape)
    return () => { window.removeEventListener('resize', position); window.removeEventListener('scroll', position, true); document.removeEventListener('pointerdown', dismiss); document.removeEventListener('keydown', escape) }
  }, [open])
  return <span className="context-help-control" ref={root} onPointerEnter={event => { if (event.pointerType === 'mouse') setOpen(true) }} onPointerLeave={() => { if (!pinned.current) setOpen(false) }} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) close() }}>
    <button type="button" ref={button} className="help-button" aria-label={label} aria-expanded={open} aria-controls={id} aria-describedby={open ? id : undefined} onClick={() => { if (pinned.current) close(); else { pinned.current = true; setOpen(true) } }}>?</button>
    {open && <span ref={popover} id={id} role="tooltip" className="context-help-popover">{text}</span>}
  </span>
}
