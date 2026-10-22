import { Link } from 'react-router-dom'
import type { RoomCard } from '../api/discovery'
import { categoryLabel } from '../lib/categories'
import { roomTopic, topicIcon } from '../lib/topics'
import AnchoredPopover from './AnchoredPopover'
import BookmarkButton from './BookmarkButton'
import Icon from './Icon'

const progressLabels = { 'not-started': 'Not started', 'in-progress': 'In progress', completed: 'Completed' }

export default function CatalogRoomCard({ room, learner, returnTo }: { room: RoomCard; learner: boolean; returnTo: string }) {
  const topics = [...new Set(room.categories?.length ? room.categories : [room.category])].filter(Boolean)
  const link = `/rooms/${room.id}`, returnState = { discovery: returnTo }, description = room.description || 'Open this content for its public briefing.'
  return <article className={`room-card catalog-room-card difficulty-${room.difficulty}`}>
    <div className="catalog-room-body">
      <div className="room-card-top"><span className={`topic-icon topic-${roomTopic(room.category)}`} aria-hidden="true"><Icon name={topicIcon(room.category)} size={23} /></span><span className="meta-pill room-type-label">{room.room_type === 'THEORY' ? 'Theory Room' : 'Practical Lab'}</span><span className={`diff-tag diff-${room.difficulty}`}>{room.difficulty}</span></div>
      <Link className="catalog-room-briefing" to={link} state={returnState}><h2 className="room-card-title" title={room.name}>{room.name}</h2><p className="room-card-desc" title={description}>{description}</p></Link>
      <div className="catalog-room-topics" aria-label="Room topics">{topics.slice(0, 2).map(topic => <span key={topic} className="meta-pill" title={categoryLabel(topic)}>{categoryLabel(topic)}</span>)}
        {topics.length > 2 && <AnchoredPopover className="room-topic-disclosure" buttonClassName="more-topics" label={`+${topics.length - 2}`} accessibleLabel={`All topics for ${room.name}`}>
          {close => <><p className="room-topics-heading">All topics</p><ul className="room-topics-list">{topics.map(topic => <li key={topic}>{categoryLabel(topic)}</li>)}</ul><button className="btn btn-ghost btn-sm" type="button" onClick={close}>Done</button></>}
        </AnchoredPopover>}
      </div>
    </div>
    <div className="catalog-room-metadata">{room.points_total != null && <span className="room-points">{room.points_total} points</span>}{room.progress && <span className={`room-progress progress-${room.progress}`}>{progressLabels[room.progress]}</span>}</div>
    <div className="room-card-actions"><Link className="card-open" to={link} state={returnState}>Open room <Icon name="arrow" size={16} /></Link><BookmarkButton kind="rooms" id={room.id} known={learner ? room.bookmarked : undefined} title={room.name} /></div>
  </article>
}
