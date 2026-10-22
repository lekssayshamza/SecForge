import { hasLearningAccess } from '../lib/permissions'
import { useEffect } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient, useIsMutating } from '@tanstack/react-query'
import { useAuth } from '../context/AuthContext'
import { bookmarkState, setBookmark, type ContentKind } from '../api/discovery'
import { authLink } from '../lib/returnTo'
export default function BookmarkButton({ kind, id, known, title }: { kind: ContentKind; id: number; known?: boolean; title: string }) {
  const { user } = useAuth(), client = useQueryClient(), key = ['bookmark', user?.id, kind, id], mutationKey = ['bookmark-write', user?.id, kind, id]
  const state = useQuery({ queryKey: key, queryFn: () => bookmarkState(kind, id), enabled: hasLearningAccess(user) && known === undefined, initialData: known === undefined ? undefined : { bookmarked: known }, staleTime: 30_000, retry: false })
  useEffect(() => { if (known !== undefined) client.setQueryData(['bookmark', user?.id, kind, id], { bookmarked: known }) }, [known, client, user?.id, kind, id])
  const pending = useIsMutating({ mutationKey }) > 0
  const mutation = useMutation({ mutationKey, mutationFn: (value: boolean) => setBookmark(kind, id, value), onSuccess: value => { client.setQueryData(key, value); void client.invalidateQueries({ queryKey: ['discovery'] }); void client.invalidateQueries({ queryKey: ['saved'] }) }, retry: false })
  if (!user) return <Link className="btn btn-ghost btn-sm bookmark-button" to={authLink('/login', `/${kind}/${id}`)} aria-label={`Log in to save ${title}`}>Save</Link>
  if (!hasLearningAccess(user)) return null
  const saved = state.data?.bookmarked ?? known
  return <div className="bookmark-control"><button type="button" className={`btn btn-ghost btn-sm bookmark-button${saved ? ' is-saved' : ''}`} aria-label={`${saved ? 'Remove' : 'Save'} ${title}${saved ? ' from saved content' : ''}`} aria-pressed={!!saved} disabled={pending || saved === undefined} onClick={() => mutation.mutate(!saved)}>{pending ? 'Saving…' : saved ? '✓ Saved' : 'Save'}</button>{(state.isError || mutation.isError) && <span className="form-hint" role="alert">Unable to update saved content. <button type="button" className="text-action" onClick={() => { if (state.isError) void state.refetch(); else mutation.mutate(!saved) }}>Retry</button></span>}</div>
}
