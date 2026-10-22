import WorkspaceShell from '../components/WorkspaceShell'
import { hasCreatorAccess } from '../lib/permissions'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { listRooms, createRoom, deleteRoom } from '../api/rooms'
import { actionError, filterRooms } from '../lib/presentation'
import Icon from '../components/Icon'
import { roomTopic, topicIcon } from '../lib/topics'
import Dialog from '../components/Dialog'
import CategoryMultiSelect from '../components/CategoryMultiSelect'
import { categoryLabel } from '../lib/categories'
import { useConfirm } from '../components/ConfirmProvider'
import type { RoomCreate } from '../types/api'

export default function Dashboard({ catalogOnly = false }: { catalogOnly?: boolean }) {
  const { user } = useAuth()
  const confirm = useConfirm()
  const queryClient = useQueryClient()
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [search, setSearch] = useState('')
  const [difficulty, setDifficulty] = useState('')
  const [status, setStatus] = useState('')
  const [category, setCategory] = useState('')
  const [deleteError, setDeleteError] = useState('')
  const { data: rooms = [], isLoading, error, refetch, isFetching } = useQuery({ queryKey: ['rooms'], queryFn: listRooms })
  const deleteMutation = useMutation({ mutationFn: deleteRoom, onSuccess: () => queryClient.invalidateQueries({ queryKey: ['rooms'] }), onError: error => setDeleteError(actionError(error, 'delete the room')) })
  if (!user) return null
  const creator = hasCreatorAccess(user)
  const filtered = filterRooms(rooms, search, difficulty, status, category)
  const categories = [...new Set(rooms.flatMap(room => room.categories?.length ? room.categories : [room.category]))].sort()
  const CatalogHeading = catalogOnly ? 'h1' : 'h2'
  const clear = () => { setSearch(''); setDifficulty(''); setStatus(''); setCategory('') }
  return <WorkspaceShell className="catalog-page">
    
    {!catalogOnly && <section className="overview-welcome"><div><p className="eyebrow">{creator ? 'Creator studio' : 'Learning workspace'}</p><h1>Welcome back, {user.display_name || user.username}</h1></div>{!creator && <Link to="/rooms" className="btn btn-primary">Explore rooms <Icon name="arrow" size={16} /></Link>}</section>}
    <header className="page-toolbar"><div><CatalogHeading className="page-heading">{creator ? 'My rooms' : 'Explore rooms'}</CatalogHeading></div>
      {creator && <button type="button" className="btn btn-primary" onClick={() => setShowCreateModal(true)}><Icon name="plus" size={18} /> Create room</button>}
    </header>
    <div className="catalog-filters"><label className="search-field"><Icon name="search" /><span className="sr-only">Search rooms</span><input type="search" placeholder="Search rooms, topics or descriptions…" value={search} onChange={e => setSearch(e.target.value)} /></label>
      <select aria-label="Filter by difficulty" value={difficulty} onChange={e => setDifficulty(e.target.value)}><option value="">All difficulties</option><option value="easy">Easy</option><option value="medium">Medium</option><option value="hard">Hard</option></select>
      <select aria-label="Filter by category" value={category} onChange={e => setCategory(e.target.value)}><option value="">All topics</option>{categories.map(c => <option key={c} value={c}>{categoryLabel(c)}</option>)}</select>
      {creator && <select aria-label="Filter by publication status" value={status} onChange={e => setStatus(e.target.value)}><option value="">All statuses</option><option value="draft">Draft</option><option value="published">Published</option></select>}
      <button type="button" className="btn btn-ghost" aria-label="Refresh rooms" onClick={() => void refetch()} disabled={isFetching}><Icon name="refresh" /></button>
    </div>
    {deleteError && <div className="error" role="alert">{deleteError}</div>}
    {isLoading ? <div className="room-grid" aria-label="Loading rooms" role="status">{[1,2,3].map(i => <div className="room-skeleton" key={i}><div /><div /><div /></div>)}</div> : error ? <div className="empty-state panel" role="alert"><Icon name="network" size={32} /><h2>We couldn’t load your rooms</h2><p>{actionError(error, 'load rooms')}</p><button type="button" className="btn btn-secondary" onClick={() => void refetch()}>Try again</button></div> : <>
      <p className="results-count" role="status">{filtered.length} {filtered.length === 1 ? 'room' : 'rooms'}{search || difficulty || category || status ? ' matching your filters' : ' to explore'}</p>
      {!filtered.length && <div className="empty-state panel"><Icon name={rooms.length ? 'search' : 'layers'} size={36} /><h2>{rooms.length ? 'No matching rooms' : creator ? 'Your first challenge starts here' : 'New challenges are on their way'}</h2><p>{rooms.length ? 'Try another keyword or broaden your filters.' : creator ? 'Create browser lessons or configure a private practical challenge.' : 'Published rooms will appear here when available.'}</p>{rooms.length ? <button type="button" className="btn btn-secondary" onClick={clear}>Clear filters</button> : creator && <button type="button" className="btn btn-primary" onClick={() => setShowCreateModal(true)}>Create room</button>}</div>}
      <ul className="room-grid">{filtered.map(room => <li key={room.id}><article className={`room-card difficulty-${room.difficulty}`}>
        <Link to={`/rooms/${room.id}`} className="room-card-main"><div className="room-card-top"><span className={`topic-icon topic-${roomTopic(room.category)}`} aria-hidden="true"><Icon name={topicIcon(room.category)} size={23} /></span><span className={`diff-tag diff-${room.difficulty}`}>{room.difficulty}</span>{creator && <span className={`badge badge-${room.status}`}>{room.status.replace('_', ' ')}</span>}</div><h2 className="room-card-title">{room.name}</h2><p className="room-card-desc">{room.description || 'Open this room for its challenge briefing.'}</p><div className="room-card-meta">{(room.categories?.length ? room.categories : [room.category]).map(category => <span className="meta-pill" key={category}>{categoryLabel(category)}</span>)}<span className="meta-pill">{room.room_type === 'THEORY' ? 'Theory' : 'Practical'}</span></div></Link>
        <div className="room-card-actions"><Link className="card-open" to={`/rooms/${room.id}`} aria-label={`Open ${room.name}`}>{creator ? 'Manage room' : 'View challenge'}<Icon name="arrow" size={17} /></Link>{creator && room.creator_id === user.id && room.status === 'draft' && <button type="button" className="btn btn-ghost btn-sm" disabled={deleteMutation.isPending} onClick={async () => { if (await confirm({ title: 'Delete draft room?', message: `“${room.name}” will be deleted. This cannot be undone.`, label: 'Delete room', danger: true })) { setDeleteError(''); deleteMutation.mutate(room.id) } }} aria-label={`Delete ${room.name}`}>Delete</button>}</div>
      </article></li>)}</ul>
    </>}
    {showCreateModal && <CreateRoomModal onClose={() => setShowCreateModal(false)} />}
  </WorkspaceShell>
}

function CreateRoomModal({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState(''), [description, setDescription] = useState(''), [selectedCategories, setSelectedCategories] = useState<string[]>([]), [difficulty, setDifficulty] = useState('easy')
  const [roomType, setRoomType] = useState<'THEORY' | 'PRACTICAL'>('PRACTICAL')
  const queryClient = useQueryClient(), navigate = useNavigate()
  const createMutation = useMutation({ mutationFn: (data: RoomCreate) => createRoom(data), onSuccess: room => { queryClient.invalidateQueries({ queryKey: ['rooms'] }); onClose(); navigate(`/rooms/${room.id}`) } })
  return <Dialog title="Create a challenge room" onClose={onClose} busy={createMutation.isPending}><form onSubmit={e => { e.preventDefault(); createMutation.mutate({ name: name.trim(), description, category: selectedCategories[0], categories: selectedCategories, difficulty, room_type: roomType }) }}>
    <div className="modal-body"><p className="dialog-copy">Start with a draft. Choose the learning format; it cannot be changed after creation.</p><div className="form-group"><label htmlFor="room-type">Room type</label><select id="room-type" value={roomType} onChange={e => setRoomType(e.target.value as 'THEORY' | 'PRACTICAL')} disabled={createMutation.isPending}><option value="PRACTICAL">Practical Lab</option><option value="THEORY">Theory Room</option></select><p className="form-hint">{roomType === 'THEORY' ? 'Browser-only lessons and quizzes. No machine, image or VPN required.' : 'Configure a private machine over SSH, save and test an image, then publish that version.'}</p></div>{createMutation.error && <div className="error" role="alert">{actionError(createMutation.error, 'create the room')}</div>}
      <div className="form-group"><label htmlFor="name">Room name</label><input id="name" value={name} onChange={e => setName(e.target.value)} required maxLength={100} placeholder="e.g. The forgotten web server" autoFocus disabled={createMutation.isPending} /></div>
      <div className="form-group"><label htmlFor="description">Challenge briefing</label><textarea id="description" value={description} onChange={e => setDescription(e.target.value)} rows={4} maxLength={2000} placeholder="Describe learning objectives and prerequisites." aria-describedby="briefing-public-help" disabled={createMutation.isPending} /><p id="briefing-public-help" className="form-hint">Public when published. Keep answers, credentials and private connection details out of this briefing.</p></div>
      <div className="form-row"><div className="form-group"><label htmlFor="category">Topic</label><CategoryMultiSelect id="category" values={selectedCategories} onChange={setSelectedCategories} disabled={createMutation.isPending} /></div><div className="form-group"><label htmlFor="difficulty">Difficulty</label><select id="difficulty" value={difficulty} onChange={e => setDifficulty(e.target.value)} disabled={createMutation.isPending}><option value="easy">Easy</option><option value="medium">Medium</option><option value="hard">Hard</option></select></div></div>
    </div><footer className="modal-footer"><button type="button" className="btn btn-secondary" onClick={onClose} disabled={createMutation.isPending}>Cancel</button><button type="submit" className="btn btn-primary" disabled={createMutation.isPending || !name.trim() || !selectedCategories.length}>{createMutation.isPending ? 'Creating…' : 'Create draft room'}</button></footer>
  </form></Dialog>
}
