import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { getPublicRoom } from '../api/rooms'
import DiscoveryCatalog from '../components/DiscoveryCatalog'
import BookmarkButton from '../components/BookmarkButton'
import { categoryLabel } from '../lib/categories'
import { useLocation } from 'react-router-dom'
import { discoveryReturn } from '../hooks/useDiscoveryState'
import { authLink } from '../lib/returnTo'

export function PublicCatalog() { return <DiscoveryCatalog /> }

export function PublicRoomDetail() {
  const location = useLocation()
  const { roomId } = useParams<{ roomId: string }>()
  const id = Number(roomId), valid = Boolean(roomId && /^[1-9][0-9]*$/.test(roomId) && Number.isSafeInteger(id) && id <= 2147483647)
  const query = useQuery({ queryKey: ['public-room', id], queryFn: () => getPublicRoom(id), enabled: valid, retry: false })
  const room = query.data, destination = `/rooms/${id}`
  if (valid && query.isPending) return <div className="loading" role="status"><span className="spinner" aria-hidden="true" /> Opening room…</div>
  if (!valid || query.isError || !room) return <div className="workspace"><div className="empty-state panel" role="alert"><h1>Room unavailable</h1><p>This room isn’t available for public browsing, or could not be loaded. Try again shortly.</p><Link className="btn btn-secondary" to="/rooms">Explore rooms</Link></div></div>
  return <div className="workspace public-room-detail"><Link className="auth-return" to={discoveryReturn(location.state?.discovery, '/rooms')}>← Explore rooms</Link>
    <header className="page-toolbar"><div><h1 className="page-heading">{room.name}</h1><div className="room-card-meta"><span className={`diff-tag diff-${room.difficulty}`}>{room.difficulty}</span>{(room.categories?.length ? room.categories : [room.category]).map(category => <span className="meta-pill" key={category}>{categoryLabel(category)}</span>)}<span className="meta-pill">{room.room_type === 'THEORY' ? 'Theory Room' : 'Practical Lab'}</span></div></div></header>
    <section className="panel public-room-briefing" aria-labelledby="public-briefing"><h2 id="public-briefing">Challenge briefing</h2><p className="public-description">{room.description || 'This room has no additional briefing yet.'}</p>{room.objectives && <div className="education-copy"><h3>Learning objectives</h3><p>{room.objectives}</p></div>}{room.prerequisites && <div className="education-copy"><h3>Prerequisites</h3><p>{room.prerequisites}</p></div>}</section>
    <BookmarkButton kind="rooms" id={room.id} title={room.name} />
    <section className="panel public-room-start" aria-labelledby="public-start"><div><h2 id="public-start">Ready to take part?</h2><p>{room.room_type === 'THEORY' ? 'Sign in to read lessons, answer quizzes and save your progress. No VPN required.' : 'Sign in to access questions, save your progress and start your private lab.'}</p></div><div className="public-room-actions"><Link className="btn btn-primary" to={authLink('/login', destination)}>Log in to start this room</Link><Link className="btn btn-secondary" to={authLink('/register', destination)}>Create an account</Link></div></section>
  </div>
}
