import { Navigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { useAuth } from '../context/AuthContext'
import { getRelationship } from '../api/social'
import Profile from './Profile'
import { PublicLearnerProfile } from './Leaderboard'

/** Public identity is shared; private data always comes from authenticated /me APIs. */
export default function CanonicalProfile() {
  const { username = '' } = useParams(), { user, isLoading } = useAuth()
  const identity = useQuery({
    queryKey: ['profile-ownership', user?.id, username],
    queryFn: () => getRelationship(username),
    enabled: Boolean(user) && !isLoading,
    retry: false, staleTime: 0,
  })
  if (isLoading || (user && identity.isPending)) return <div className="workspace" role="status">Opening profile…</div>
  if (user && identity.data?.is_self) return <Profile />
  return <>{user && identity.isError && <div className="workspace"><p role="alert" className="error">Profile ownership could not be checked. Private controls are unavailable. <button className="btn btn-ghost" onClick={() => void identity.refetch()}>Retry</button></p></div>}<PublicLearnerProfile /></>
}

export function ProfileAlias() {
  const { user, isLoading } = useAuth()
  if (isLoading) return <div className="workspace" role="status">Opening profile…</div>
  return <Navigate to={user ? `/learners/${encodeURIComponent(user.username)}` : '/login'} replace />
}
