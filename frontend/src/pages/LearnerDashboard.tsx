import { Link } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { useLearning } from '../hooks/useLearning'
import Icon from '../components/Icon'
import { categoryLabel } from '../lib/categories'
import { LearningMetrics, LearningRooms, ActivityList, Achievements } from '../components/LearningPanels'
export default function LearnerDashboard() {
  const { user } = useAuth(), query = useLearning(), data = query.data
  if (!user) return null
  const next = data?.continue_learning[0]
  return <div className="workspace learner-overview"><header className="learner-welcome"><div><p className="eyebrow">Your learning workspace</p><h1>Welcome back, {user.username}</h1><p>Pick up an investigation or find your next challenge.</p></div><Link className="btn btn-primary" to={next ? `/rooms/${next.id}` : '/rooms'}>{next ? 'Continue learning' : 'Explore rooms'} <Icon name="arrow" size={17} /></Link></header>
    {query.isPending ? <div className="loading" role="status"><span className="spinner" aria-hidden="true" /> Loading your learning overview…</div> : query.isError || !data ? <div className="error" role="alert">Your learning overview is unavailable. <button className="btn btn-ghost" onClick={() => void query.refetch()}>Try again</button></div> : <>
      <LearningMetrics stats={data.stats} />
      <div className="learning-saved-link"><Link className="btn btn-secondary" to="/saved">Your saved rooms and learning paths <Icon name="arrow" size={16} /></Link></div>
      <div className="learning-columns"><section className="panel learning-section"><header className="section-toolbar"><h2>Continue learning</h2><Link to="/rooms">Explore rooms</Link></header><LearningRooms rooms={data.continue_learning} empty={data.stats.rooms_in_progress ? 'Your saved progress is retained, but those rooms are not currently published. Explore rooms for an available challenge.' : 'No rooms in progress yet. Your first correct answer creates saved progress you can return to.'} /></section><section className="panel learning-section"><h2>Recent activity</h2><ActivityList items={data.activity} /></section></div>
      <section className="panel learning-section"><header className="section-toolbar"><div><h2>Your next investigation</h2><p className="muted">{data.recommendation_label}</p></div><Link to="/rooms">Explore all</Link></header>{data.recommendations.length ? <div className="recommendation-grid">{data.recommendations.map(room => <article key={room.id}><div className="room-card-top"><span className={`diff-tag diff-${room.difficulty}`}>{room.difficulty}</span><span className="meta-pill">{categoryLabel(room.category)}</span></div><h3><Link to={`/rooms/${room.id}`}>{room.name}</Link></h3><p>{room.description || 'Open the room for its challenge briefing.'}</p><Link className="card-open" to={`/rooms/${room.id}`}>View room <Icon name="arrow" size={15} /></Link></article>)}</div> : <p className="learning-empty">New cybersecurity rooms are coming soon. Published challenges will appear here when available.</p>}</section>
      <div className="learning-columns"><Achievements badges={data.achievements} compact /><section className="panel learning-section"><h2>Recently completed</h2><LearningRooms rooms={data.completed_rooms} completed empty="Completed rooms will appear here after you solve all their questions." /></section></div>
    </>}
  </div>
}
