import type { ReactNode } from 'react'
import Avatar from './Avatar'
import ContextHelp from './ContextHelp'
import type { User } from '../types/api'
export default function ProfileIdentity({ person, label, children }: {
  person: Pick<User, 'username' | 'display_name' | 'avatar_style' | 'avatar_color'> & Partial<Pick<User, 'id' | 'avatar_version' | 'bio'>> & { avatar_url?: string | null }
  label: string
  children?: ReactNode
}) {
  return <header className="panel identity-header"><Avatar user={person} large /><div className="identity-copy"><p className="eyebrow">{label}</p><h1>{person.display_name || person.username}</h1><p className="muted">@{person.username}</p>{person.bio && <p className="identity-bio">{person.bio}</p>}</div>{children}</header>
}
export function ProfileMetrics({ stats }: { stats: { points: number; rooms_completed: number; questions_solved: number; rooms_in_progress?: number } }) {
  const values: [string, number][] = [['Total points', stats.points], ['Rooms completed', stats.rooms_completed]]
  if (stats.rooms_in_progress !== undefined) values.push(['Rooms in progress', stats.rooms_in_progress])
  values.push(['Questions solved', stats.questions_solved])
  return <dl className="learning-metrics">{values.map(([name, value]) => <div key={name}><dt>{name}{name === 'Total points' && <ContextHelp label="About points" text="Points come from correct answers in other creators’ published rooms. Each question awards points once." />}</dt><dd>{value.toLocaleString()}</dd></div>)}</dl>
}
