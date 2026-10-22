import { useQuery } from '@tanstack/react-query'
import { useAuth } from '../context/AuthContext'
import { getProgress } from '../api/challenges'
import type { Room } from '../types/api'
import Icon from './Icon'

export default function RoomLearningProgress({ room }: { room: Room }) {
  const { user } = useAuth()
  const progress = useQuery({ queryKey: ['progress', room.id, user?.id], queryFn: () => getProgress(room.id) })
  if (progress.isLoading) return <p role="status" className="muted">Loading progress…</p>
  if (!progress.data) return <p className="error" role="alert">Progress unavailable. <button className="btn btn-ghost btn-sm" onClick={() => void progress.refetch()}>Retry</button></p>
  const p = progress.data
  const percent = p.questions_total ? Math.min(100, Math.round(100 * p.questions_solved / p.questions_total)) : 0
  const complete = p.completed && p.questions_total > 0 && p.questions_solved === p.questions_total
  return <section className={`learner-progress${complete ? ' learner-completed panel' : ''}`} aria-label="Room progress" role="status">
    {complete ? <><Icon name="check" size={26} /><div><h2>Room completed!</h2><p>You’ve completed all {p.questions_total} challenges in {room.name}.</p><div className="learner-progress-meta"><strong>Progress: {percent}%</strong><strong>Points earned: {p.points_earned}</strong></div><p className="muted">Your progress and points are saved.</p></div></> : <><span>{p.questions_solved}/{p.questions_total} solved</span><progress className="room-progress-bar" max={100} value={percent} aria-label="Room completion progress" /><strong>{percent}% complete</strong><span>{p.points_earned} points earned</span>{p.completed && <span className="muted">Earlier completion retained; updated challenges are available.</span>}</>}
    {progress.error && <p className="error" role="alert">Progress could not be refreshed. <button className="btn btn-ghost btn-sm" onClick={() => void progress.refetch()}>Retry</button></p>}
  </section>
}
