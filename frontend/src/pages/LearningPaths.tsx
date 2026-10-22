import WorkspaceShell from '../components/WorkspaceShell'
import { hasCreatorAccess, hasLearningAccess } from '../lib/permissions'
import { lazy, Suspense, useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams, useLocation } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useAuth } from '../context/AuthContext'
import { createPath, getPath, getPathProgress, listPaths, type PathWrite } from '../api/paths'
import { actionError } from '../lib/presentation'
import { authLink } from '../lib/returnTo'
import { useUnsavedChanges, confirmDiscard } from '../hooks/useUnsavedChanges'
import Dialog from '../components/Dialog'
import Icon from '../components/Icon'
import SafeMarkdown from '../components/SafeMarkdown'
import PathFields from '../components/PathFields'
import { categoryLabel } from '../lib/categories'
import DiscoveryCatalog from '../components/DiscoveryCatalog'
import BookmarkButton from '../components/BookmarkButton'
import ContextHelp from '../components/ContextHelp'
import { discoveryReturn } from '../hooks/useDiscoveryState'

const PathCreator = lazy(() => import('../components/PathCreator'))


const blank: PathWrite = { title: '', description: '', category: '', difficulty: 'easy', objectives: '' }
export default function LearningPaths({ mine = false }: { mine?: boolean }) { return mine ? <CreatorPaths mine /> : <DiscoveryCatalog kind="paths" /> }
function CreatorPaths({ mine = true }: { mine?: boolean }) {
  const { user } = useAuth(), navigate = useNavigate()
  const [search, setSearch] = useState(''), [q, setQ] = useState(''), [difficulty, setDifficulty] = useState(''), [category, setCategory] = useState(''), [page, setPage] = useState(1), [creating, setCreating] = useState(false)
  const allowed = !mine || hasCreatorAccess(user)
  useEffect(() => { const t = setTimeout(() => { setQ(search.trim()); setPage(1) }, 250); return () => clearTimeout(t) }, [search])
  const query = useQuery({ queryKey: ['paths', mine, user?.id, q, difficulty, category, page], queryFn: () => listPaths({ q, difficulty, category, page }, mine), enabled: allowed, retry: false })
  if (!allowed) return <div className="workspace"><h1>Creator access required</h1><Link to="/paths">Explore learning paths</Link></div>
  return <WorkspaceShell className="paths-page"><header className="page-toolbar"><div><p className="eyebrow">A route through cybersecurity</p><h1 className="page-heading">{mine ? 'My learning paths' : 'Learning paths'}</h1><p className="muted">Build knowledge step by step with ordered lessons and practical challenges.</p></div><div className="button-row">{mine ? <button className="btn btn-primary" onClick={() => setCreating(true)}>Create path</button> : hasCreatorAccess(user) && <Link className="btn btn-secondary" to="/paths/mine">Manage paths</Link>}{mine && <Link className="btn btn-secondary" to="/paths">Browse paths</Link>}</div></header>
    {!mine && <div className="catalog-filters"><label className="search-field"><Icon name="search" /><span className="sr-only">Search learning paths</span><input type="search" maxLength={100} value={search} onChange={e => setSearch(e.target.value)} placeholder="Search subjects and learning paths…" /></label><select aria-label="Path difficulty" value={difficulty} onChange={e => { setDifficulty(e.target.value); setPage(1) }}><option value="">All levels</option><option value="easy">Beginner</option><option value="medium">Intermediate</option><option value="hard">Advanced</option></select><select aria-label="Path category" value={category} onChange={e => { setCategory(e.target.value); setPage(1) }}><option value="">All subjects</option>{query.data?.categories.map(c => <option key={c}>{c}</option>)}</select></div>}
    {query.isPending ? <p role="status" className="inline-loading">Loading learning paths…</p> : query.isError ? <div className="error" role="alert">{actionError(query.error, 'load learning paths')} <button className="btn btn-secondary" onClick={() => void query.refetch()}>Try again</button></div> : <>
      {!query.data?.items.length && <section className="panel empty-state"><Icon name="compass" size={36} /><h2>{q || category || difficulty ? 'No matching paths' : mine ? 'Create a learning journey' : 'New learning paths are coming soon'}</h2><p>{mine ? 'Organize existing rooms into a clear recommended order.' : 'Explore rooms while creators prepare structured courses.'}</p><Link className="btn btn-secondary" to="/rooms">Explore rooms</Link></section>}
      <ul className="path-grid">{query.data?.items.map(path => <li key={path.id}><article className="panel path-card"><div className="path-card-heading"><span className="path-emblem" aria-hidden="true"><Icon name="compass" size={27} /></span><span className={`diff-tag diff-${path.difficulty}`}>{path.difficulty}</span>{mine && <span className={`badge badge-${path.status}`}>{path.status}</span>}</div><h2>{path.title}</h2><p className="muted">{path.description || 'Follow a structured sequence of cybersecurity rooms.'}</p><div className="path-card-footer"><span className="meta-pill">{categoryLabel(path.category)}</span><span>{path.room_count} published rooms</span><Link className="btn btn-secondary" to={`/paths/${path.id}${mine ? '/edit' : ''}`}>{mine ? 'Edit path' : 'View journey'} <Icon name="arrow" size={16} /></Link></div></article></li>)}</ul>
      {!!query.data && query.data.total > query.data.page_size && <nav className="public-pagination" aria-label="Path pages"><button className="btn btn-secondary" disabled={page <= 1} onClick={() => setPage(p => p - 1)}>Previous</button><span>Page {page}</span><button className="btn btn-secondary" disabled={page >= 1000 || page * query.data.page_size >= query.data.total} onClick={() => setPage(p => p + 1)}>Next</button></nav>}
    </>}{creating && <CreatePath onClose={() => setCreating(false)} onCreated={id => navigate(`/paths/${id}/edit`)} />}</WorkspaceShell>
}
function CreatePath({ onClose, onCreated }: { onClose: () => void; onCreated: (id: number) => void }) {
  const [data, setData] = useState(blank), [pending, setPending] = useState(false), [error, setError] = useState(''), busy = useRef(false)
  const dirty = JSON.stringify(data) !== JSON.stringify(blank); useUnsavedChanges(dirty)
  const close = () => { if (confirmDiscard(dirty)) onClose() }
  const submit = async (e: FormEvent) => { e.preventDefault(); if (busy.current) return; busy.current = true; setPending(true); try { const p = await createPath(data); onCreated(p.id) } catch (err) { setError(actionError(err, 'create this path')) } finally { setPending(false); busy.current = false } }
  return <Dialog title="Step 1 of 4 · Create learning path" onClose={close} busy={pending}><form onSubmit={e => void submit(e)}><div className="modal-body">{error && <p role="alert" className="error">{error}</p>}<p className="form-hint">Start with the details. Next, choose rooms, arrange their order and preview your path.</p><PathFields data={data} onChange={setData} disabled={pending} /></div><footer className="modal-footer"><button type="button" className="btn btn-secondary" onClick={close} disabled={pending}>Cancel</button><button className="btn btn-primary" disabled={pending}>{pending ? 'Creating…' : 'Create and add rooms'}</button></footer></form></Dialog>
}
export function LearningPathDetail({ edit = false }: { edit?: boolean }) {
  const location = useLocation()
  const { pathId } = useParams(), { user } = useAuth(), id = Number(pathId)
  const allowed = !edit || hasCreatorAccess(user), valid = Number.isSafeInteger(id) && id > 0 && id <= 2147483647
  const query = useQuery({ queryKey: ['path', id, edit, user?.id], queryFn: () => getPath(id, edit), enabled: valid && allowed, retry: false, refetchOnWindowFocus: !edit })
  const progress = useQuery({ queryKey: ['path-progress', id, user?.id], queryFn: () => getPathProgress(id), enabled: valid && !edit && hasLearningAccess(user) && !!query.data, retry: false, staleTime: 0 })
  if (!allowed || !valid) return <div className="workspace"><h1>Path unavailable</h1><Link to="/paths">Explore paths</Link></div>
  if (query.isPending) return <div className="workspace" role="status">Opening learning path…</div>
  if (!query.data || query.isError) return <div className="workspace"><div className="panel empty-state" role="alert"><h1>Path unavailable</h1><p>It may be private or temporarily unavailable.</p><Link className="btn btn-secondary" to="/paths">Explore paths</Link></div></div>
  if (edit) return <Suspense fallback={<div className="workspace" role="status">Opening path editor…</div>}><PathCreator key={id} path={query.data} /></Suspense>
  const path = query.data, p = progress.data, next = p?.next_room_id ?? (!p ? path.rooms[0]?.id : null)
  return <div className="workspace path-detail"><Link className="auth-return" to={discoveryReturn(location.state?.discovery, '/paths')}>← Learning paths</Link><header className="path-intro panel"><span className="eyebrow">Recommended learning journey</span><h1 className="page-heading">{path.title}</h1><div className="room-card-meta"><span className={`diff-tag diff-${path.difficulty}`}>{path.difficulty}</span><span className="meta-pill">{categoryLabel(path.category)}</span><span>{path.room_count} rooms</span></div><p className="education-copy">{path.description}</p>{path.objectives && <section className="education-copy"><h2>Learning objectives</h2><SafeMarkdown text={path.objectives} /></section>}<ContextHelp label="About path progress" text="Follow the recommended order, or open any available room. Only saved completions count; opening a page does not award credit." />
    {p && <div className="path-progress" role="status"><strong>{p.completed ? 'Path completed' : `${p.rooms_completed} of ${p.rooms_total} rooms completed`}</strong><progress aria-label="Path completion" value={p.percent} max={100} /><span>{p.percent}%</span></div>}{progress.isError && <p role="alert" className="error">Progress is temporarily unavailable. <button className="btn btn-ghost" onClick={() => void progress.refetch()}>Retry progress</button></p>}
    {next && <Link className="btn btn-primary" to={user ? `/rooms/${next}?path=${id}` : authLink('/login', `/paths/${id}`)}>{user ? p?.rooms_completed ? 'Continue learning' : 'Start learning' : 'Log in to start learning'} <Icon name="arrow" size={16} /></Link>}</header>
    <BookmarkButton kind="paths" id={path.id} title={path.title} />
    {!path.rooms.length && <section className="panel empty-state"><h2>No rooms are currently available</h2><p>Some rooms may be undergoing updates. Check back soon.</p></section>}
    <ol className="path-journey">{path.rooms.map((room, i) => { const solved = p?.completed_room_ids.includes(room.id); return <li key={room.id} className={solved ? 'is-complete' : ''}><span className="journey-step" aria-label={`Room ${i + 1}`}>{solved ? <Icon name="check" size={18} /> : String(i + 1).padStart(2, '0')}</span><div className="panel journey-room"><div className="room-card-meta"><span className="meta-pill">{room.room_type === 'THEORY' ? 'Theory Room' : 'Practical Lab'}</span><span className={`diff-tag diff-${room.difficulty}`}>{room.difficulty}</span>{solved && <span className="badge badge-published">Completed</span>}</div><h2><Link to={`/rooms/${room.id}?path=${id}`}>{room.name}</Link></h2><p className="muted">{room.description}</p><Link className="btn btn-secondary btn-sm" to={`/rooms/${room.id}?path=${id}`}>{solved ? 'Review room' : 'Open room'}</Link></div></li> })}</ol></div>
}
