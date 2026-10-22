import { Link } from 'react-router-dom'
import Icon from './Icon'
import type { LearningStats, Achievement, LearningRoom, LearningActivity } from '../types/api'
export function LearningMetrics({ stats }: { stats: LearningStats }) {
  return <dl className="learning-metrics">{[['Total points',stats.points],['Rooms completed',stats.rooms_completed],['Rooms in progress',stats.rooms_in_progress],['Questions solved',stats.questions_solved]].map(([name,value]) => <div key={name}><dt>{name}</dt><dd>{Number(value).toLocaleString()}</dd></div>)}</dl>
}
export function Achievements({ badges, compact = false }: { badges: Achievement[]; compact?: boolean }) {
  const earned = badges.filter(b => b.earned), shown = compact ? earned : badges
  return <section className="panel learning-section"><header className="section-toolbar"><h2>Achievements</h2>{compact && <Link to="/profile">View profile <Icon name="arrow" size={14} /></Link>}</header>
    {!earned.length && <p className="muted">No achievements earned yet. Solve a question to make your first discovery.</p>}
    <div className="achievement-grid">{shown.map(badge => <article className={`achievement${badge.earned ? ' is-earned' : ' is-locked'}`} key={badge.id}><span className="achievement-symbol" aria-hidden="true"><Icon name={badge.earned ? 'check' : 'key'} size={22} /></span><div><h3>{badge.name}</h3><p>{badge.description}</p><span className="muted">{badge.earned ? `Earned ${new Date(badge.earned_at!).toLocaleDateString()}` : `Locked · ${badge.progress}/${badge.target}`}</span></div></article>)}</div>
  </section>
}
export function LearningRooms({ rooms, empty, completed = false }: { rooms: LearningRoom[]; empty: string; completed?: boolean }) {
  return rooms.length ? <ul className="learning-room-list">{rooms.map(room => <li key={room.id}><span className="learning-room-icon" aria-hidden="true"><Icon name={completed ? 'check' : 'terminal'} /></span><div className="learning-room-copy"><h3>{room.available ? <Link to={`/rooms/${room.id}`}>{room.name}</Link> : room.name}</h3><p className="muted">{completed ? `${room.points} points · ${new Date(room.completed_at!).toLocaleDateString()}` : `${room.solved} questions solved${room.questions ? ` of ${room.questions}` : ''}`}</p>{room.percentage !== null && !completed && <><progress max={100} value={room.percentage} aria-label={`${room.name}: ${room.percentage}% complete`} /><span className="muted">{room.percentage}% complete</span></>}</div>{room.available && <Link className="btn btn-secondary btn-sm" to={`/rooms/${room.id}`}>{completed ? 'Review' : 'Resume'}</Link>}</li>)}</ul> : <p className="learning-empty">{empty}</p>
}
export function ActivityList({ items }: { items: LearningActivity[] }) {
  return items.length ? <ol className="learning-activity">{items.map(item => <li key={item.id}><span className="activity-dot" aria-hidden="true" /><div><p>{item.kind === 'answer' ? 'Solved a question in ' : 'Completed '}{item.available ? <Link to={`/rooms/${item.room_id}`}>{item.room_name}</Link> : item.room_name}</p><span className="muted">{new Date(item.occurred_at).toLocaleString()}{item.kind === 'answer' ? ` · +${item.points} points` : ''}</span></div></li>)}</ol> : <p className="learning-empty">Your correct answers and completed rooms will appear here.</p>
}
