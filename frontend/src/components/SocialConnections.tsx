import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient, useIsMutating } from '@tanstack/react-query'
import { useAuth } from '../context/AuthContext'
import { getConnections, getRelationship, listConnections, setRelationship } from '../api/social'
import { authLink } from '../lib/returnTo'
import { actionError } from '../lib/presentation'
import Avatar from './Avatar'
import Dialog from './Dialog'
import { useConfirm } from './ConfirmProvider'
export default function SocialConnections({ username }: { username: string }) {
  const { user } = useAuth(), client = useQueryClient(), confirm = useConfirm(), [list, setList] = useState<'followers' | 'following' | null>(null)
  const key = ['social', username, user?.id], mutationKey = ['social-write', user?.id, username]
  const counts = useQuery({ queryKey: ['connections', username], queryFn: () => getConnections(username), staleTime: 15_000, retry: false })
  const state = useQuery({ queryKey: key, queryFn: () => getRelationship(username), enabled: !!user, staleTime: 15_000, retry: false })
  const pending = useIsMutating({ mutationKey }) > 0
  const mutation = useMutation({ mutationKey, mutationFn: ({ action, enabled }: { action: 'follow' | 'block'; enabled: boolean }) => setRelationship(username, action, enabled), retry: false,
    onSuccess: result => { client.setQueryData(key, result); void client.invalidateQueries({ queryKey: ['connections'] }); void client.invalidateQueries({ queryKey: ['social'] }); void client.invalidateQueries({ queryKey: ['connection-list'] }) } })
  const own = user?.username.toLowerCase() === username.toLowerCase(), data = state.data ?? counts.data
  return <section className="panel social-connections" aria-label="Profile connections"><div className="social-counts">{(['followers', 'following'] as const).map(direction => <button type="button" className="btn btn-ghost" key={direction} onClick={() => setList(direction)} disabled={!data}><strong>{data?.[direction] ?? '—'}</strong> {direction === 'followers' ? 'Followers' : 'Following'}</button>)}</div>
    {!own && <div className="button-row">{!user ? <Link className="btn btn-primary btn-sm" to={authLink('/login', `/learners/${encodeURIComponent(username)}`)}>Log in to follow</Link> : <>
      {!state.data?.blocked && <button type="button" className={`btn btn-sm ${state.data?.following_user ? 'btn-secondary' : 'btn-primary'}`} disabled={pending || !state.data || !state.data.can_follow} aria-pressed={state.data?.following_user ?? false} onClick={() => mutation.mutate({ action: 'follow', enabled: !state.data?.following_user })}>{pending ? 'Updating…' : state.data?.following_user ? 'Unfollow' : 'Follow'}</button>}
      <button type="button" className="btn btn-ghost btn-sm" disabled={pending || !state.data} onClick={async () => { const blocked = state.data?.blocked ?? false; if (blocked || await confirm({ title: `Block @${username}?`, message: 'This removes follows in both directions and prevents new follows. Public profiles remain visible. Unblocking does not restore follows.', label: 'Block user', danger: true })) mutation.mutate({ action: 'block', enabled: !blocked }) }}>{state.data?.blocked ? 'Unblock' : 'Block'}</button>
      {state.data && !state.data.can_follow && !state.data.blocked && <span className="muted">Following unavailable.</span>}
    </>}</div>}
    {(counts.isError || state.isError || mutation.isError) && <p className="error" role="alert">{mutation.isError ? actionError(mutation.error, 'update connections') : 'Connections are unavailable.'}<button type="button" className="text-action" onClick={() => { void counts.refetch(); if (user) void state.refetch() }}>Refresh</button></p>}
    {list && <ConnectionList key={`${username}:${list}`} username={username} direction={list} onClose={() => setList(null)} />}
  </section>
}
function ConnectionList({ username, direction, onClose }: { username: string; direction: 'followers' | 'following'; onClose: () => void }) {
  const [page, setPage] = useState(1)
  const query = useQuery({ queryKey: ['connection-list', username, direction, page], queryFn: () => listConnections(username, direction, page), retry: false, staleTime: 15_000 })
  return <Dialog title={`${direction === 'followers' ? 'Followers' : 'Following'} · @${username}`} onClose={onClose}><div className="connections-body">{query.isPending ? <p role="status">Loading connections…</p> : query.isError || !query.data ? <p className="error" role="alert">Connections are unavailable. <button onClick={() => void query.refetch()}>Retry</button></p> : <>
    {query.data.items.length ? <ul className="connection-list">{query.data.items.map(person => <li key={person.username}><Link to={`/learners/${encodeURIComponent(person.username)}`} onClick={onClose}><Avatar user={person} decorative /><span><strong>{person.display_name || person.username}</strong><span className="muted">@{person.username}</span></span></Link></li>)}</ul> : <p className="learning-empty">{direction === 'followers' ? 'No followers yet.' : 'No accounts followed yet.'}</p>}
    {query.data.total > query.data.page_size && <nav className="public-pagination" aria-label="Connection pages"><button className="btn btn-secondary" disabled={page === 1 || query.isFetching} onClick={() => setPage(p => p - 1)}>Previous</button><span>Page {page}</span><button className="btn btn-secondary" disabled={page >= 1000 || page * query.data.page_size >= query.data.total || query.isFetching} onClick={() => setPage(p => p + 1)}>Next</button></nav>}
  </>}</div></Dialog>
}
