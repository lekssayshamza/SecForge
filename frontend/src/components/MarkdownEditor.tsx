import { useEffect, useRef, useState } from 'react'
import SafeMarkdown from './SafeMarkdown'
import SecureRoomImage from './SecureRoomImage'
import { listLessonImages, uploadLessonImage, removeLessonImage, type LessonImages } from '../api/roomMedia'
import { actionError } from '../lib/presentation'
export default function MarkdownEditor({ roomId, value, onChange, onBusy, disabled = false }: { roomId: number; value: string; onChange: (value: string) => void; onBusy: (busy: boolean) => void; disabled?: boolean }) {
  const input = useRef<HTMLTextAreaElement>(null), fileInput = useRef<HTMLInputElement>(null), upload = useRef<AbortController | null>(null)
  const [preview, setPreview] = useState(true), [images, setImages] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState(''), [alt, setAlt] = useState(''), [library, setLibrary] = useState<LessonImages | null>(null)
  const [page, setPage] = useState(1), [reload, setReload] = useState(0)
  useEffect(() => () => upload.current?.abort(), [])
  useEffect(() => {
    if (!images) return
    let active = true
    void listLessonImages(roomId, page).then(data => { if (active) setLibrary(data) }).catch(err => { if (active) setError(actionError(err, 'load images')) })
    return () => { active = false }
  }, [images, page, roomId, reload])
  const insert = (prefix: string, suffix = '', placeholder = '') => {
    const field = input.current, start = field?.selectionStart ?? value.length, end = field?.selectionEnd ?? start
    const selected = value.slice(start, end) || placeholder, next = value.slice(0, start) + prefix + selected + suffix + value.slice(end)
    if (next.length > 12000) { setError('Instructions can contain at most 12,000 characters.'); return }
    onChange(next)
    requestAnimationFrame(() => { field?.focus(); field?.setSelectionRange(start + prefix.length, start + prefix.length + selected.length) })
  }
  const imageMarkdown = (url: string) => `\n![${alt.trim().replace(/[\[\]\\\r\n]/g, ' ')}](${url})\n`
  const addImage = async (file: File) => {
    if (busy || disabled) return
    if (!alt.trim() || alt.trim().length > 240) { setError('Describe the image first (1–240 characters).'); return }
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type) || file.size > 2 * 1024 * 1024) { setError('Choose a PNG, JPEG or WebP image up to 2 MB.'); return }
    setBusy(true); onBusy(true); setError(''); upload.current = new AbortController()
    try { const item = await uploadLessonImage(roomId, file, upload.current.signal); insert(imageMarkdown(item.url)); setReload(n => n + 1) }
    catch (err) { setError(actionError(err, 'upload this image')) }
    finally { setBusy(false); onBusy(false); upload.current = null; if (fileInput.current) fileInput.current.value = '' }
  }
  const remove = async (id: string) => {
    if (busy) return
    setBusy(true); onBusy(true); setError('')
    try { await removeLessonImage(roomId, id); setReload(n => n + 1) }
    catch (err) { setError(actionError(err, 'remove this image')) }
    finally { setBusy(false); onBusy(false) }
  }
  return <div className="markdown-editor">
    <div className="markdown-toolbar" role="group" aria-label="Instruction formatting">
      {[['Heading', '\n## ', '', 'Heading'], ['Bold', '**', '**', 'text'], ['Italic', '*', '*', 'text'], ['List', '\n- ', '', 'Item'], ['Code', '\n```sh\n', '\n```\n', 'command'], ['Quote', '\n> ', '', 'Quote'], ['Table', '\n| Heading | Heading |\n| --- | --- |\n| Cell | Cell |\n', '', '']].map(([label, before, after, example]) => <button key={label} type="button" className="btn btn-secondary btn-sm" disabled={disabled || busy} onMouseDown={e => e.preventDefault()} onClick={() => insert(before, after, example)}>{label}</button>)}
      <button type="button" className="btn btn-secondary btn-sm" aria-expanded={images} disabled={disabled || busy} onClick={() => setImages(!images)}>Images</button>
      <button type="button" className="btn btn-secondary btn-sm" aria-expanded={preview} onClick={() => setPreview(!preview)}>{preview ? 'Hide preview' : 'Show preview'}</button>
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    {images && <section className="lesson-image-panel" aria-label="Lesson image library">
      <label htmlFor="lesson-image-alt">Describe the image (alt text)</label><input id="lesson-image-alt" maxLength={240} value={alt} disabled={busy || disabled} onChange={e => setAlt(e.target.value)} placeholder="For example: Linux filesystem hierarchy" />
      <p className="form-hint">PNG, JPEG or WebP, up to 2 MB and 4096 pixels per side. Images remain private until explicitly published in a lesson. Only signed-in room readers can view published lesson images.</p>
      <input ref={fileInput} type="file" accept="image/png,image/jpeg,image/webp" aria-label="Upload lesson image" disabled={busy || disabled || !alt.trim()} onChange={e => { const file = e.target.files?.[0]; if (file) void addImage(file) }} />
      {busy && <p role="status">Updating image library…</p>}
      <div className="lesson-image-library">{library?.items.map(item => <article key={item.id}><SecureRoomImage src={item.url} alt="Uploaded lesson illustration" /><span>{item.width} × {item.height}</span><button type="button" className="btn btn-secondary btn-sm" disabled={busy || disabled || !alt.trim()} onClick={() => insert(imageMarkdown(item.url))}>Insert image</button><button type="button" className="btn btn-danger btn-sm" disabled={busy || disabled || item.in_use || value.includes(item.url)} onClick={() => void remove(item.id)}>Remove unused image</button></article>)}</div>
      {library?.total === 0 && <p>No images uploaded yet.</p>}{library && library.total > library.page_size && <nav aria-label="Image library pages"><button type="button" disabled={page <= 1 || busy} onClick={() => setPage(n => n - 1)}>Previous</button><span>Page {page}</span><button type="button" disabled={page * library.page_size >= library.total || busy} onClick={() => setPage(n => n + 1)}>Next</button></nav>}
    </section>}
    <label htmlFor="task-instructions">Learning material &amp; instructions</label><textarea ref={input} id="task-instructions" rows={10} maxLength={12000} value={value} disabled={disabled || busy} onChange={e => onChange(e.target.value)} />
    <p className="form-hint">Protected learner content. Keep solutions out of instructions.</p>
    <details><summary>Formatting guide</summary><p>Use # headings, **bold**, *italic*, lists, &gt; quotes, `inline code` and fenced code blocks. Tables use | columns | and a | --- | separator row. Two trailing spaces make a line break. HTTP/HTTPS links are supported. Raw HTML and external images are disabled.</p></details>
    {preview && <section className="task-preview" aria-label="Instruction preview"><SafeMarkdown text={value} /></section>}
  </div>
}
